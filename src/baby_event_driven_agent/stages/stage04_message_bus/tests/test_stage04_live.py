"""Stage 4 真模型用例：打本地 ollama。

只留两条：一条验异步分发真的在异步（慢观测者追不上），一条回归——
换了传输层之后，stage03 的中断语义没被改坏。
其余传输层行为都在 test_stage04_transport.py 里离线验过了。

前置：仓库根 .env 配好 OPENAI_API_BASE（本地 ollama）与 OPENAI_MODEL。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage04_message_bus.agent import (
    STOP_CLOSER,
    STOP_MARKER,
    Agent,
)
from baby_event_driven_agent.stages.stage04_message_bus.bus import EventBus
from baby_event_driven_agent.stages.stage04_message_bus.events import (
    Event,
    Subscription,
    UserMessage,
)
from baby_event_driven_agent.stages.stage04_message_bus.llm import RealLLM
from baby_event_driven_agent.stages.stage04_message_bus.outbound import StreamConsumer
from baby_event_driven_agent.stages.stage04_message_bus.subscribers import counter

TIMEOUT = 120.0
TRACKED = (
    "user_input",
    "agent_reply",
    "tool_call_started",
    "tool_result",
    "turn_end",
)


class StreamRecorder(StreamConsumer):
    """stream 消费者（MAILBOX_ONLY 要求）：记录每条事件 + 合并缓冲计数。"""

    def __init__(self) -> None:
        super().__init__(max_chars=96, frame=0.05)
        self.events: list[Event] = []

    def offer(self, event: Event) -> None:
        self.events.append(event)
        super().offer(event)

    def on_flush(self, text: str) -> None:
        pass


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage04_live_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Harness:
    """bus + agent + 记录器：stream 走 StreamRecorder，其余走 await 型。"""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.agent = Agent(self.bus, RealLLM())
        self.stream = StreamRecorder()
        self.seen_events: list[Event] = []
        self.q: dict[str, asyncio.Queue[Event]] = {}
        for name in TRACKED:
            self.q[name] = asyncio.Queue()
            self.bus.subscribe(
                Subscription(f"rec-{name}", (name,), self._recorder(name))
            )
        self.bus.subscribe(
            Subscription("rec-stream", ("agent_delta", "agent_thinking"), self.stream)
        )

    def _recorder(self, name: str):
        async def rec(event: Event) -> None:
            self.seen_events.append(event)
            self.q[name].put_nowait(event)

        return rec

    async def wait(self, name: str) -> Event:
        return await asyncio.wait_for(self.q[name].get(), TIMEOUT)

    def send(self, text: str, sid: str = "A") -> None:
        self.bus.publish(UserMessage("user_input", sid, {"text": text}), to=self.agent.agent_id)

    def interrupt(self, sid: str = "A", intent: str = "stop") -> None:
        self.bus.publish(
            Event("user_interrupt", sid, {"intent": intent}), to=self.agent.agent_id
        )

    async def stop(self) -> None:
        await self.agent.stop()


def test_turn_delivers_stream_and_lifecycle(workdir: Path) -> None:
    """一轮真对话：token 增量与生命周期事件都到达消费者，一条不丢。"""
    h = Harness()
    seen: dict[str, int] = {}
    h.bus.subscribe(counter(seen, session="A"))

    async def go() -> Event:
        h.send("保温杯还有库存吗")
        end = await h.wait("turn_end")
        await h.stop()
        return end

    end = asyncio.run(go())
    assert end.payload.get("reason") == "turn end"
    assert seen.get("turn_end") == 1  # 生命周期事件一条没丢
    assert len(h.stream.events) > 0  # token 增量也逐条到达 StreamConsumer


def test_interrupt_semantics_survive_the_transport_rewrite(workdir: Path) -> None:
    """回归：换了传输层，stage03 的“掐掉在飞的一步、turn 以 interrupted 收尾”照旧。"""
    h = Harness()

    async def go() -> Event:
        h.send("报销有什么规定")
        try:
            await h.wait("tool_call_started")  # step 确定在飞
        except asyncio.TimeoutError:
            pass
        h.interrupt(intent="stop")
        end = await h.wait("turn_end")
        await h.stop()
        return end

    end = asyncio.run(go())
    assert end.payload.get("reason") == "interrupted", end.payload
    hist = h.agent.history["A"]
    assert hist[-1]["content"] in (STOP_MARKER, STOP_CLOSER), hist[-1]
    # 合成消息也走事件出（否则订阅者重建不出这段 history）
    assert any(ev.payload.get("synthetic") for ev in h.seen_events), [
        ev.payload for ev in h.seen_events
    ]
