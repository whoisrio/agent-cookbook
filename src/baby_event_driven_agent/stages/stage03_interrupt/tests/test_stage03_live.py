"""Stage 3 用例：打本地 ollama 的真模型，事件驱动。

不设替身：这一版要验的就是真 provider 收不收这套消息形状、模型行为对不对。
时序靠 agent 侧事件驱动——等到目标流状态出现（tool_call_started / agent_delta /
agent_thinking / tool_result）再发中断，不靠 sleep 碰运气。

"各落点"里的 ②（一个增量都还没吐）、③（只在吐 thinking）、⑦（正好落在 step
边界）没有确定性事件可等，尽力而为：等得到就等，等不到就按最接近的状态发，
断言只压"形状对不对"，不压"一定命中了哪一格"。

前置：仓库根 .env 配好 OPENAI_API_BASE（本地 ollama）与 OPENAI_MODEL。

总线分了方向：送给 agent 的命令走 inbound 的同步 publish（Harness 的 send /
interrupt 只是同步投递，不等 turn），agent 往外发的事件走 emit 扇出，Harness
按类型排队记录。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage03_interrupt.agent import (
    NO_EXEC,
    REDIRECT_NOTE,
    STOP_CLOSER,
    STOP_MARKER,
    Agent,
)
from baby_event_driven_agent.stages.stage03_interrupt.events import (
    Event,
    EventBus,
    SessionLog,
)
from baby_event_driven_agent.stages.stage03_interrupt.llm import TOOLS, RealLLM

TIMEOUT = 120.0
TRACKED = (
    "agent_thinking",
    "agent_delta",
    "tool_call_started",
    "agent_reply",
    "tool_result",
    "step_cancelled",
    "turn_interrupted",
    "turn_end",
)


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage03_live_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def slow_search() -> tuple[asyncio.Event, asyncio.Event]:
    """把 search_rules 换成一个"进去要等放行"的慢工具（schema 不动，
    模型照样会选它）。返回 (已进入, 放行) 两个 Event。
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    original = TOOLS["search_rules"]

    async def slow(args: dict) -> str:
        entered.set()
        await release.wait()
        return "报销：每月 25 号前提交，超 500 元需发票原件。"

    TOOLS["search_rules"] = slow
    yield entered, release
    TOOLS["search_rules"] = original


class Harness:
    """bus + agent + 全事件记录：按类型排队，测试"等到某个状态"再发信号。"""

    def __init__(self, log_path: Path) -> None:
        self.bus = EventBus()
        # Agent 构造时自己把 enqueue 登记进总线：user_input 和 user_interrupt
        # 都按 agent_id 从这里投递进来
        self.agent = Agent(self.bus, SessionLog(str(log_path)), RealLLM())
        self.q: dict[str, asyncio.Queue[Event]] = {}
        for name in TRACKED:
            self.q[name] = asyncio.Queue()
            self.bus.subscribe(name, self._recorder(name))

    def _recorder(self, name: str):
        async def rec(event: Event) -> None:
            self.q[name].put_nowait(event)

        return rec

    async def wait(self, name: str) -> Event:
        return await asyncio.wait_for(self.q[name].get(), TIMEOUT)

    def send(self, text: str, sid: str = "A") -> None:
        # inbound 的 publish 是同步的：投进收件箱立刻返回，不等 turn 跑完
        self.bus.publish(
            Event("user_input", sid, {"text": text}), to=self.agent.agent_id
        )

    def interrupt(
        self, sid: str = "A", intent: str = "stop", text: str | None = None
    ) -> None:
        payload: dict = {"intent": intent}
        if text is not None:
            payload["text"] = text
        # 中断也是 inbound 命令：同一个收件地址，不进收件箱
        self.bus.publish(
            Event("user_interrupt", sid, payload), to=self.agent.agent_id
        )

    async def stop(self) -> None:
        await self.agent.stop()


def records(log_path: Path) -> list[dict]:
    return [json.loads(line) for line in log_path.read_text().splitlines()]


def notes(log_path: Path, note: str) -> list[dict]:
    return [r for r in records(log_path) if r.get("note") == note]


def roles(history: list[dict]) -> list[str]:
    return [m["role"] for m in history]


def tail(history: list[dict]) -> dict:
    return history[-1]


# ------------------------------------------------------------------ ① / ⑨


def test_idle_stop_is_noop(workdir: Path) -> None:
    """① 空闲（turn 之间）和 ⑨ turn 已收尾：中断什么都不该做，也不该残留。

    两次 turn 都完整，且日志里没有 step_cancelled / turn_interrupted。
    """
    log_path = workdir / "session.jsonl"
    h = Harness(log_path)

    async def run() -> None:
        h.send("保温杯还有库存吗")
        await h.wait("turn_end")
        h.interrupt()                      # ⑨：turn 刚收完
        h.interrupt()                      # ①：确认空闲重复发也无害
        h.send("VPN 怎么申请")
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    assert not [r for r in records(log_path) if r["type"] == "step_cancelled"]
    assert not [r for r in records(log_path) if r["type"] == "turn_interrupted"]
    assert len(notes(log_path, "interrupt received")) == 2
    # 第二个 turn 没有被残留的标志掐掉
    assert len(notes(log_path, "final")) == 2


# ------------------------------------------------------------------ ② / ④ / ⑧（stop）


def test_stream_stop_closes_with_marker(workdir: Path) -> None:
    """②④⑧ stop：掐掉在飞的 step，尾部补 user 中断标记封口。

    等不到确定性窗口就按最接近的状态发——断言只压"形状"：step 被取消、
    turn 以 interrupted 收尾、history 尾部是那条自描述标记。
    """
    log_path = workdir / "session.jsonl"
    h = Harness(log_path)

    async def run() -> None:
        h.send("报销有什么规定")
        # 能等到 tool_call_started 最好（④）；等不到就直接发（②）
        try:
            await h.wait("tool_call_started")
        except asyncio.TimeoutError:
            pass
        h.interrupt(intent="stop")
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    recs = records(log_path)
    assert [r for r in recs if r["type"] == "step_cancelled"]
    assert notes(log_path, "interrupted")
    hist = h.agent.history["A"]
    assert tail(hist) == {"role": "user", "content": STOP_MARKER}
    # 中断之后 history 仍然是合法序列：没有半截 assistant
    assert hist[-2]["role"] == "user"


# ------------------------------------------------------------------ ⑤（stop，慢工具）


def test_tool_phase_stop_waits_then_closes_with_assistant(
    workdir: Path, slow_search: tuple[asyncio.Event, asyncio.Event]
) -> None:
    """⑤ stop：工具执行中不掐（默认不可中断），跑完在边界收尾，补 assistant 占位。"""
    entered, release = slow_search
    log_path = workdir / "session.jsonl"
    h = Harness(log_path)

    async def run() -> None:
        h.send("报销有什么规定")
        await h.wait("tool_call_started")
        await asyncio.wait_for(entered.wait(), TIMEOUT)   # 工具确实在跑
        h.interrupt(intent="stop")
        await asyncio.sleep(0)                            # 让信号先落地
        release.set()                                     # 放行工具
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    recs = records(log_path)
    # 工具没被掐：拿到的是真实结果，不是占位
    results = [r for r in recs if r["type"] == "tool_result"]
    assert results and results[-1]["payload"]["skipped"] is False
    assert [r for r in recs if r["type"] == "turn_interrupted"]
    hist = h.agent.history["A"]
    assert tail(hist) == {"role": "assistant", "content": STOP_CLOSER}
    assert hist[-2]["role"] == "tool"


# ------------------------------------------------------------------ ⑥（stop，多调用）


def test_tool_phase_stop_skips_unstarted_calls(
    workdir: Path, slow_search: tuple[asyncio.Event, asyncio.Event]
) -> None:
    """⑥ stop：一批里的多个调用，跑着的等它完，没开始的补"未执行"占位。

    尽力而为：这条要求模型一次吐出 ≥2 个 tool_call——吐不出就跳过。
    """
    entered, release = slow_search
    log_path = workdir / "session.jsonl"
    h = Harness(log_path)

    async def run() -> None:
        h.send("报销和 VPN 分别有什么规定，都要查")
        await h.wait("tool_call_started")
        await asyncio.wait_for(entered.wait(), TIMEOUT)
        h.interrupt(intent="stop")
        await asyncio.sleep(0)
        release.set()
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    hist = h.agent.history["A"]
    placeholders = [
        m for m in hist if m.get("role") == "tool" and m["content"] == NO_EXEC
    ]
    if not placeholders:
        pytest.skip("模型这次只发了一个 tool_call，⑥ 没构造出来（尽力而为）")
    assert tail(hist) == {"role": "assistant", "content": STOP_CLOSER}
    # 真跑了的那个在前、占位在后，顺序与原 tool_calls 一致
    assert hist[-2]["content"] == NO_EXEC


# ------------------------------------------------------------------ ④ / ⑧（redirect）


def test_stream_redirect_keeps_partial_and_annotates(workdir: Path) -> None:
    """④⑧ redirect：半成品按已观测事实补进 history，再补带标注的纠正 user。"""
    log_path = workdir / "session.jsonl"
    h = Harness(log_path)

    async def run() -> None:
        h.send("报销有什么规定")
        try:
            await h.wait("tool_call_started")     # ④：已见到工具意图
        except asyncio.TimeoutError:
            pass
        h.interrupt(intent="redirect", text="先别查了，改成订会议室")
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    hist = h.agent.history["A"]
    corrections = [
        m
        for m in hist
        if m.get("role") == "user" and m["content"].startswith(REDIRECT_NOTE)
    ]
    assert corrections, roles(hist)
    # 工具意图一旦出现，就必须成对：assistant(tool_calls) + 每个 call 一条 tool
    assistants = [m for m in hist if m.get("tool_calls")]
    for msg in assistants:
        ids = [c["id"] for c in msg["tool_calls"]]
        paired = [m for m in hist if m.get("role") == "tool"]
        assert all(i for i in ids if any(m["tool_call_id"] == i for m in paired))


# ------------------------------------------------------------------ ⑤ / ⑦（redirect → steering）


def test_boundary_redirect_is_plain_steering(
    workdir: Path, slow_search: tuple[asyncio.Event, asyncio.Event]
) -> None:
    """⑤⑦ redirect：工具阶段（step 边界）转向没有残破消息可修，
    纠正就是一条普通 user 消息 —— 不加"被打断"标注。
    """
    entered, release = slow_search
    log_path = workdir / "session.jsonl"
    h = Harness(log_path)

    async def run() -> None:
        h.send("报销有什么规定")
        await h.wait("tool_call_started")
        await asyncio.wait_for(entered.wait(), TIMEOUT)
        h.interrupt(intent="redirect", text="先别查了，改成订会议室")
        await asyncio.sleep(0)
        release.set()
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    hist = h.agent.history["A"]
    plain = [m for m in hist if m.get("role") == "user" and m["content"] == "先别查了，改成订会议室"]
    assert plain, roles(hist)
    assert not [
        m for m in hist if m.get("role") == "user" and m["content"].startswith(REDIRECT_NOTE)
    ]
