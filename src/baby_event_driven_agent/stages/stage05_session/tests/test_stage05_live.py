"""Stage 5a 真模型用例：打 .env 配的模型（本地 ollama 也能跑）。

只留两条：
1. 一轮真对话后，轨迹落在盘上且投影合法（轨迹层的主路径）；
2. 回归——history 换成轨迹层之后，stage03/04 的中断语义没被改坏，
   且合成消息进的是轨迹（不只是事件流）。
其余都在 test_stage05_trajectory / test_stage05_agent / test_stage05_session
里离线验过了。

前置：仓库根 .env 配好 OPENAI_API_BASE（本地 ollama）与 OPENAI_MODEL。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage05_session.agent import (
    STOP_CLOSER,
    STOP_MARKER,
    Agent,
)
from baby_event_driven_agent.stages.stage05_session.transport.bus import EventBus
from baby_event_driven_agent.stages.stage05_session.transport.events import (
    OBSERVE,
    Event,
    Subscription,
)
from baby_event_driven_agent.stages.stage05_session.llm import RealLLM
from baby_event_driven_agent.stages.stage05_session.transport.persistence import EventLog
from baby_event_driven_agent.stages.stage05_session.session.store import SessionStore

TIMEOUT = 120.0


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage05_live_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Harness:
    def __init__(self, workdir: Path) -> None:
        self.log = EventLog(str(workdir / "events"))
        self.bus = EventBus(self.log)
        self.store = SessionStore(workdir / "sessions")
        self.agent = Agent(self.bus, RealLLM(), store=self.store)
        self.traj = self.store.start()
        self.sid = self.agent.attach(self.traj)
        self.ended = asyncio.Event()
        self.bus.subscribe(
            Subscription("rec-end", ("turn_end",), self._on_end, mode=OBSERVE)
        )

    async def _on_end(self, event: Event) -> None:
        self.ended.set()

    def send(self, text: str) -> None:
        self.bus.publish(Event("user_input", self.sid, {"text": text}), to=self.agent.agent_id)

    def interrupt(self, intent: str = "stop") -> None:
        self.bus.publish(
            Event("user_interrupt", self.sid, {"intent": intent}), to=self.agent.agent_id
        )

    async def wait_turn(self) -> Event:
        return await asyncio.wait_for(self.ended.wait(), TIMEOUT)

    async def stop(self) -> None:
        await self.agent.stop()


def test_turn_lands_on_disk_and_projects_legally(workdir: Path) -> None:
    """一轮真对话：轨迹文件里是合法序列；投影出合法 messages（含工具结果）。"""
    h = Harness(workdir)

    async def go() -> None:
        h.send("保温杯还有库存吗")
        await h.wait_turn()
        await h.bus.drain(timeout=30.0)
        await h.stop()

    asyncio.run(asyncio.wait_for(go(), TIMEOUT))

    entries = h.traj.entries()
    roles = [
        str(e.payload["message"].get("role")) for e in entries if e.type == "message"
    ]
    assert roles and roles[0] == "user"
    # 序列合法性：tool 结果永远紧跟在带 tool_calls 的 assistant 后面
    for i, role in enumerate(roles):
        if role == "tool":
            assert roles[i - 1] == "assistant"
    # 生命周期事实在轨迹里：start 与 close
    types = [e.type for e in entries]
    assert types[0] == "session_started"

    projection = build_context(h.traj)
    assert projection.messages[0]["role"] == "system"
    assert projection.messages[1]["content"] == "保温杯还有库存吗"
    assert projection.messages[-1]["role"] == "assistant"
    # 重启重放：从盘上重建的轨迹投影出同样的 messages
    from baby_event_driven_agent.stages.stage05_session.agent import build_context
    from baby_event_driven_agent.stages.stage05_session.session.trajectory import Trajectory, TrajectoryLog

    reloaded = Trajectory.load(TrajectoryLog(h.store.path_of(h.sid)))
    assert [m["content"] for m in build_context(reloaded).messages if m.get("content")] == [
        m["content"] for m in projection.messages if m.get("content")
    ]


def test_interrupt_semantics_survive_the_trajectory_rewrite(workdir: Path) -> None:
    """回归：掐掉在飞的一步，turn 以 interrupted 收尾；合成消息进轨迹。"""
    h = Harness(workdir)

    async def go() -> None:
        h.send("报销有什么规定")
        await asyncio.sleep(0.3)  # step 确定在飞
        h.interrupt(intent="stop")
        await h.wait_turn()
        await h.bus.drain(timeout=30.0)
        await h.stop()

    asyncio.run(asyncio.wait_for(go(), TIMEOUT))
    last = h.traj.last_message()
    assert last is not None and last["content"] in (STOP_MARKER, STOP_CLOSER)
    # 合成消息进的是轨迹（事实层），不只是事件流
    synth = [
        e for e in h.traj.entries()
        if e.type == "message" and e.payload.get("synthetic")
    ]
    assert synth and all(e.payload.get("note") for e in synth)
