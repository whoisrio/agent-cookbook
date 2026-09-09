"""Stage 2 测试：收件箱投递、followup 排队、steering step 边界生效。

离线跑：用与 RealLLM 同协议的 FakeLLM，first_call_gate 把
"消息在 step 在飞时到达"变成确定性时序。不用 pytest 的 tmp_path
fixture（WorkBuddy 沙箱 shim 会拦 pytest-of-unknown 的 mkdir），
用 tempfile.mkdtemp 自建临时目录，测试结束自己清理。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage02_inbox_steering.agent import Agent
from baby_event_driven_agent.stages.stage02_inbox_steering.events import (
    Event,
    EventBus,
    SessionLog,
)
from baby_event_driven_agent.stages.stage02_inbox_steering.llm import FakeLLM

TIMEOUT = 5.0


@pytest.fixture()
def log_path() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage02_test_"))
    yield d / "session.jsonl"
    shutil.rmtree(d, ignore_errors=True)


class Harness:
    """bus + agent + turn_end 计数，测试共用。

    turn_end 走 Queue 缓存而不是单个 Event——多个 worker 并发收尾时
    Event 的 set/clear 会互相吃掉对方的信号，Queue 不会丢。
    """

    def __init__(self, log_path: Path, llm: FakeLLM) -> None:
        self.bus = EventBus()
        self.agent = Agent(self.bus, SessionLog(str(log_path)), llm)
        self.bus.subscribe("user_input", self.agent.on_user_input)
        self.turn_ends = 0
        self.turn_end_q: asyncio.Queue[Event] = asyncio.Queue()
        self.bus.subscribe("turn_end", self._on_turn_end)

    async def _on_turn_end(self, e: Event) -> None:
        self.turn_ends += 1
        self.turn_end_q.put_nowait(e)

    async def send(self, text: str, sid: str = "A") -> None:
        await self.bus.publish(Event("user_input", sid, {"text": text}))

    async def wait_turn(self) -> None:
        await asyncio.wait_for(self.turn_end_q.get(), TIMEOUT)

    async def stop(self) -> None:
        await self.agent.stop()


def test_handler_returns_before_turn_finishes(log_path: Path) -> None:
    """核心主张：handler 只投递立刻返回，turn 不再占着总线回调。"""
    h = Harness(log_path, FakeLLM())

    async def run() -> None:
        await h.send("第一问")
        # publish 已返回，但 worker 还没跑到 turn_end——投递与执行解耦
        assert h.turn_ends == 0
        await h.wait_turn()
        assert h.turn_ends == 1
        await h.stop()

    asyncio.run(run())


def test_followup_starts_next_turn(log_path: Path) -> None:
    """worker 空闲后取到的消息 = followup：作为新 turn 的输入。"""
    h = Harness(log_path, FakeLLM())

    async def run() -> None:
        await h.send("第一问")
        await h.wait_turn()
        await h.send("第二问")
        await h.wait_turn()
        await h.stop()

    asyncio.run(run())

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    starts = [r for r in records if r.get("note") == "turn start"]
    assert len(starts) == 2
    assert starts[0]["payload"]["text"] == "第一问"
    assert starts[1]["payload"]["text"] == "第二问"
    # 没有任何消息被标成 steering
    assert not [r for r in records if r.get("note") == "steering"]


def test_steering_folds_into_running_turn(log_path: Path) -> None:
    """turn 在跑时到达的消息在下一个 step 边界被 drain，拼进当前上下文。

    turn 至少要有两步才有 step 边界——FakeLLM 第一步发起 search，
    插话在第一步在飞时投进 inbox，第二步开始前被 drain。
    """
    gate = asyncio.Event()
    h = Harness(
        log_path, FakeLLM(first_call_gate=gate, first_call_tool=True)
    )
    consumed: list[Event] = []

    async def collect(e: Event) -> None:
        consumed.append(e)

    h.bus.subscribe("steering_consumed", collect)

    async def run() -> None:
        await h.send("第一问")
        # FakeLLM 第一次 stream_chat 已启动并停在 gate 上——step 正在飞
        await asyncio.sleep(0.05)
        await h.send("插话")
        gate.set()  # 放行 step 1（tool_call）；step 2 开始前 drain 捞到"插话"
        await h.wait_turn()
        await h.stop()

    asyncio.run(run())

    # drain 消费时发出 steering_consumed，UI/demo 靠它可视化
    assert [e.payload["texts"] for e in consumed] == [["插话"]]

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    steerings = [r for r in records if r.get("note") == "steering"]
    assert len(steerings) == 1
    assert steerings[0]["payload"]["text"] == "插话"
    # steering 不开新 turn：整轮只有一次 turn start，一次 turn end
    assert len([r for r in records if r.get("note") == "turn start"]) == 1
    assert len([r for r in records if r.get("note") == "turn end"]) == 1

    # history 里插话被拼成 user 消息，模型第二步看到的是它
    hist = h.agent.history["A"]
    assert hist[-2] == {"role": "user", "content": "插话"}
    # 最终回复消费的是插话内容（FakeLLM 按"最后一条消息"生成回复文本）
    finals = [r for r in records if r.get("note") == "final"]
    assert finals[-1]["payload"]["message"]["content"] == "回复：插话"


def test_message_arriving_after_last_drain_degrades_to_followup(
    log_path: Path,
) -> None:
    """临界降级：消息落在最后一个 drain 点之后，turn 收尾没带上它——
    它不该丢，也不该硬塞进已收尾的 turn，而是降级为 followup，
    worker 的下一次 inbox.get() 自动接住。"""
    gate = asyncio.Event()
    h = Harness(log_path, FakeLLM(first_call_gate=gate))  # 一步的 turn

    async def run() -> None:
        await h.send("第一问")
        await asyncio.sleep(0.05)  # step 正在飞
        await h.send("迟到的插话")
        gate.set()  # 放行唯一一步 → turn 结束，没有下一次 drain
        await h.wait_turn()  # 第一 turn（插话没被消化）
        await h.wait_turn()  # 插话作为 followup 的第二 turn
        await h.stop()

    asyncio.run(run())

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    # 没有任何消息被标成 steering
    assert not [r for r in records if r.get("note") == "steering"]
    starts = [r for r in records if r.get("note") == "turn start"]
    assert [s["payload"]["text"] for s in starts] == ["第一问", "迟到的插话"]


def test_sessions_have_independent_inboxes_and_histories(log_path: Path) -> None:
    """不同 session 各有收件箱和 worker，history 互不污染。"""
    h = Harness(log_path, FakeLLM())

    async def run() -> None:
        await h.send("A 的问题", sid="A")
        await h.send("B 的问题", sid="B")
        await h.wait_turn()
        await h.wait_turn()
        await h.stop()

    asyncio.run(run())

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert {r["session"] for r in records} == {"A", "B"}
    for sid in ("A", "B"):
        notes = [r.get("note") for r in records if r["session"] == sid]
        assert notes.count("turn start") == 1
        assert notes.count("turn end") == 1
