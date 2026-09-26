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
    INTERRUPTED,
    NO_EXEC,
    STOP_CLOSER,
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

    def interrupt(self, sid: str = "A", text: str | None = None) -> None:
        payload: dict = {"text": text} if text is not None else {}
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

    两次 turn 都完整；会话账只投影消息，生命周期事件不进账。
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

    recs = records(log_path)
    assert {r["type"] for r in recs} <= {"user_input", "agent_reply", "tool_result"}
    # 第二个 turn 没有被残留的标志掐掉
    assert len(notes(log_path, "final")) == 2


# ------------------------------------------------------------------ ② / ④ / ⑧（stop）


def test_stream_stop_closes_with_assistant(workdir: Path) -> None:
    """②④⑧ stop：掐掉在飞的 step，尾部补 assistant 打断占位封口。

    等不到确定性窗口就按最接近的状态发——断言只压"形状"：step 被取消、
    turn 以 interrupted 收尾、history 尾部是那条自描述占位
    （尾部是没被回答的 user，不补的话下一轮模型会把它翻出来重答）。
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
        h.interrupt()
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    hist = h.agent.history["A"]
    assert tail(hist) == {"role": "assistant", "content": INTERRUPTED}
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
        h.interrupt()
        await asyncio.sleep(0)                            # 让信号先落地
        release.set()                                     # 放行工具
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    recs = records(log_path)
    # 工具没被掐：拿到的是真实结果，不是占位
    results = [r for r in recs if r["type"] == "tool_result"]
    assert results and results[-1]["payload"]["skipped"] is False
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
        h.interrupt()
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


def test_tools_done_stop_closes_with_assistant_closer(workdir: Path) -> None:
    """⑤ stop：工具已经全跑完、turn 还没收尾时按 stop，收口按**尾部角色**来。

    此刻 history 尾部停在 `tool`（工具结果没人接），缺的是 assistant 收尾 →
    补 assistant 封口占位（否则尾部会漏出 `[..., tool, user]` 这种
    "工具结果没人接住"的形状）。
    """
    log_path = workdir / "session.jsonl"
    h = Harness(log_path)

    async def run() -> None:
        h.send("报销有什么规定")
        await h.wait("tool_result")   # 工具已经跑完，结果马上进 history
        h.interrupt()
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    hist = h.agent.history["A"]
    assert tail(hist) == {"role": "assistant", "content": STOP_CLOSER}
    assert hist[-2]["role"] == "tool", roles(hist)


# ------------------------------------------------------------------ ④ / ⑧（打断并附新消息）


def test_stream_interrupt_with_message_discards_incomplete_step(workdir: Path) -> None:
    """①②③ 打断并附新消息：没收到完整的 LLM 返回就当没收到——在飞 step 的产物一律丢。

    可见文本 / 半截 tool_call 都不进 history：半截的 arguments 断在半路、不是
    合法消息，也没执行过，补不了占位。收尾 = assistant 打断占位 + 新消息，
    三种未完整返回的场景同形状。
    """
    log_path = workdir / "session.jsonl"
    h = Harness(log_path)

    async def run() -> None:
        h.send("报销有什么规定")
        try:
            await h.wait("tool_call_started")     # ③：已见到工具意图（参数还没吐完）
        except asyncio.TimeoutError:
            pass
        h.interrupt(text="先别查了，改成订会议室")
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    hist = h.agent.history["A"]
    idx = next(
        i
        for i, m in enumerate(hist)
        if m.get("role") == "user" and m["content"] == "先别查了，改成订会议室"
    )
    # 收尾形状：打断占位紧跟在被丢掉的 user 问题之后，新消息是纯文本
    assert hist[idx - 1] == {"role": "assistant", "content": INTERRUPTED}
    # 占位之前的在飞产物全丢：不能出现半截 assistant(tool_calls)、也不能有它的 tool 结果
    before = hist[: idx - 1]
    assert not [m for m in before if m.get("tool_calls")], roles(hist)
    assert not [m for m in before if m.get("role") == "tool"], roles(hist)


# ------------------------------------------------------------------ ⑤ / ⑦（附新消息 → steering）


def test_boundary_message_is_plain_steering(
    workdir: Path, slow_search: tuple[asyncio.Event, asyncio.Event]
) -> None:
    """⑤⑦ 打断附新消息落在工具阶段（step 边界）：没有残破消息可修，
    新消息就是一条普通 user 消息，不加"被打断"标注。
    """
    entered, release = slow_search
    log_path = workdir / "session.jsonl"
    h = Harness(log_path)

    async def run() -> None:
        h.send("报销有什么规定")
        await h.wait("tool_call_started")
        await asyncio.wait_for(entered.wait(), TIMEOUT)
        h.interrupt(text="先别查了，改成订会议室")
        await asyncio.sleep(0)
        release.set()
        await h.wait("turn_end")
        await h.stop()

    asyncio.run(run())

    hist = h.agent.history["A"]
    plain = [m for m in hist if m.get("role") == "user" and m["content"] == "先别查了，改成订会议室"]
    assert plain, roles(hist)
    # 边界上的新消息没有残破消息可修：只补这条纯 user，不补任何 assistant 打断占位
    assert not [
        m for m in hist if m.get("role") == "assistant" and m["content"] == INTERRUPTED
    ], roles(hist)
