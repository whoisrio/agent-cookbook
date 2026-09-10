"""Stage 3 测试：中断掐掉在飞的 step，worker 与 history 完好。

离线跑：FakeLLM 的 first_call_gate 把第一步挂起，中断信号在它
等待期间到达，取消是确定性的。不用 pytest 的 tmp_path fixture
（WorkBuddy 沙箱 shim 会拦 pytest-of-unknown 的 mkdir），用
tempfile.mkdtemp 自建临时目录，测试结束自己清理。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage03_interrupt.agent import Agent
from baby_event_driven_agent.stages.stage03_interrupt.events import (
    Event,
    EventBus,
    SessionLog,
)
from baby_event_driven_agent.stages.stage03_interrupt.llm import FakeLLM

TIMEOUT = 5.0


@pytest.fixture()
def log_path() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage03_test_"))
    yield d / "session.jsonl"
    shutil.rmtree(d, ignore_errors=True)


class Harness:
    """bus + agent + turn_end / step_cancelled 收集，测试共用。

    turn_end 走 Queue 缓存而不是单个 Event——多个 turn 并发收尾时
    Event 的 set/clear 会互相吃掉对方的信号，Queue 不会丢。
    """

    def __init__(self, log_path: Path, llm: FakeLLM) -> None:
        self.bus = EventBus()
        self.agent = Agent(self.bus, SessionLog(str(log_path)), llm)
        self.bus.subscribe("user_input", self.agent.on_user_input)
        self.bus.subscribe("user_interrupt", self.agent.on_interrupt)
        self.turn_end_q: asyncio.Queue[Event] = asyncio.Queue()
        self.cancelled: list[Event] = []
        self.bus.subscribe("turn_end", self._on_turn_end)
        self.bus.subscribe("step_cancelled", self._on_cancelled)

    async def _on_turn_end(self, e: Event) -> None:
        self.turn_end_q.put_nowait(e)

    async def _on_cancelled(self, e: Event) -> None:
        self.cancelled.append(e)

    async def send(self, text: str, sid: str = "A") -> None:
        await self.bus.publish(Event("user_input", sid, {"text": text}))

    async def interrupt(self, sid: str = "A") -> None:
        await self.bus.publish(Event("user_interrupt", sid, {}))

    async def wait_turn(self) -> None:
        await asyncio.wait_for(self.turn_end_q.get(), TIMEOUT)

    async def stop(self) -> None:
        await self.agent.stop()


def test_interrupt_cancels_inflight_step_and_turn_ends(log_path: Path) -> None:
    """核心主张：中断取消正在飞的 step，turn 收尾，worker 活着。

    被取消那步的部分输出不进 history——history 尾部是发起提问的
    user 消息，之后下一条消息照常得到完整回答。
    """
    gate = asyncio.Event()
    h = Harness(log_path, FakeLLM(first_call_gate=gate))

    async def run() -> None:
        await h.send("第一问")
        await asyncio.sleep(0.05)  # 第一步已启动，停在 gate 上——正在飞
        await h.interrupt()
        await h.wait_turn()  # turn 以 interrupted 收尾
        assert not gate.is_set()  # 那一步再也不会被放行
        # 被取消的 step 没有留下任何 assistant 痕迹：尾部就是那条 user 消息
        hist = h.agent.history["A"]
        assert hist[-1] == {"role": "user", "content": "第一问"}
        await h.send("第二问")
        await h.wait_turn()
        await h.stop()

    asyncio.run(run())

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [r for r in records if r["type"] == "step_cancelled"]
    ends = [r for r in records if r.get("note") == "interrupted"]
    assert len(ends) == 1
    # 中断之后的新 turn 正常完成（FakeLLM 第二次调用直接回答）
    finals = [r for r in records if r.get("note") == "final"]
    assert finals[-1]["payload"]["message"]["content"] == "回复：第二问"


def test_interrupt_when_idle_is_noop(log_path: Path) -> None:
    """没有在飞的 step 时，中断信号落空，不影响下一个 turn。"""
    h = Harness(log_path, FakeLLM())

    async def run() -> None:
        await h.send("第一问")
        await h.wait_turn()  # turn 已结束，此刻空闲
        await h.interrupt()
        await h.send("第二问")
        await h.wait_turn()
        await h.stop()

    asyncio.run(run())

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    # 信号本身进了 log（发生过的事实），但没有 step 被取消
    assert [r for r in records if r.get("note") == "interrupt received"]
    assert not [r for r in records if r.get("note") == "step_cancelled"]
    assert not h.cancelled
    # 第二问得到的是完整回答，不是被掐断的残次品
    finals = [r for r in records if r.get("note") == "final"]
    assert finals[-1]["payload"]["message"]["content"] == "回复：第二问"


def test_queued_followups_survive_interrupt(log_path: Path) -> None:
    """中断只掐当前 turn：排队里的消息不丢，被下一个 turn 消化。

    排队二在 turn 1 在飞时到达，turn 1 被中断后它还留在收件箱里；
    turn 2 取走排队一，开始前的 step 边界 drain 把排队二当 steering
    拼进上下文（消费那一刻分类，和 stage02 语义一致）——模型在
    同一个 turn 里一起回答两条。
    """
    gate = asyncio.Event()
    h = Harness(log_path, FakeLLM(first_call_gate=gate))

    async def run() -> None:
        await h.send("第一问")
        await asyncio.sleep(0.05)  # 第一步在飞
        await h.send("排队一")
        await h.send("排队二")
        await h.interrupt()
        await h.wait_turn()  # 第一 turn 被中断
        await h.wait_turn()  # 排队一 + 排队二（steering）一个 turn 答完
        await h.stop()

    asyncio.run(run())

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    starts = [r for r in records if r.get("note") == "turn start"]
    assert [s["payload"]["text"] for s in starts] == ["第一问", "排队一"]
    steerings = [r for r in records if r.get("note") == "steering"]
    assert [s["payload"]["text"] for s in steerings] == ["排队二"]
    assert len([r for r in records if r.get("note") == "interrupted"]) == 1
    finals = [r for r in records if r.get("note") == "final"]
    assert finals[-1]["payload"]["message"]["content"] == "回复：排队二"


def test_steering_still_works_after_step_refactor(log_path: Path) -> None:
    """回归：step 重构成可取消单元后，stage02 的 steering 语义不变。"""
    gate = asyncio.Event()
    h = Harness(log_path, FakeLLM(first_call_gate=gate, first_call_tool=True))

    async def run() -> None:
        await h.send("第一问")
        await asyncio.sleep(0.05)  # 第一步在飞
        await h.send("插话")
        gate.set()  # 放行第一步（tool_call）；第二步开始前 drain 捞到插话
        await h.wait_turn()
        await h.stop()

    asyncio.run(run())

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    steerings = [r for r in records if r.get("note") == "steering"]
    assert len(steerings) == 1
    assert steerings[0]["payload"]["text"] == "插话"
    assert len([r for r in records if r.get("note") == "turn start"]) == 1
