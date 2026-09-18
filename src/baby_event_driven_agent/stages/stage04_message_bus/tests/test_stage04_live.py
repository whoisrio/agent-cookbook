"""Stage 4 真模型用例：打本地 ollama。

只留两条：一条验信封（seq / correlation_id / 异步分发真的在异步），
一条回归——换了传输层之后，stage03 的中断语义没被改坏。
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
    OBSERVE,
    Event,
    Subscription,
)
from baby_event_driven_agent.stages.stage04_message_bus.llm import RealLLM
from baby_event_driven_agent.stages.stage04_message_bus.persistence import EventLog
from baby_event_driven_agent.stages.stage04_message_bus.subscribers import (
    counter,
    slow_observer,
)

TIMEOUT = 120.0
TRACKED = ("agent_delta", "agent_reply", "tool_call_started", "tool_result", "turn_end")


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage04_live_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Harness:
    """bus + agent + 落盘 + 按类型排队的记录器（emit 是异步的，观测也是异步的）。"""

    def __init__(self, log_dir: Path) -> None:
        self.log = EventLog(str(log_dir))
        self.bus = EventBus(self.log)
        self.agent = Agent(self.bus, RealLLM())
        self.q: dict[str, asyncio.Queue[Event]] = {}
        for name in TRACKED:
            self.q[name] = asyncio.Queue()
            self.bus.subscribe(
                Subscription(f"rec-{name}", (name,), self._recorder(name), mode=OBSERVE)
            )

    def _recorder(self, name: str):
        async def rec(event: Event) -> None:
            self.q[name].put_nowait(event)

        return rec

    async def wait(self, name: str) -> Event:
        return await asyncio.wait_for(self.q[name].get(), TIMEOUT)

    def send(self, text: str, sid: str = "A") -> None:
        self.bus.publish(Event("user_input", sid, {"text": text}), to=self.agent.agent_id)

    def interrupt(self, sid: str = "A", intent: str = "stop") -> None:
        self.bus.publish(
            Event("user_interrupt", sid, {"intent": intent}), to=self.agent.agent_id
        )

    async def stop(self) -> None:
        await self.agent.stop()


def test_turn_has_envelope_and_dispatch_is_async(workdir: Path) -> None:
    """一轮真对话：事件带 seq / correlation_id，且慢订阅者没拖住 loop。

    “没拖住”的证据是滞后：turn_end 到达那一刻，慢观测者还没追上已发出的事件数。
    事件太少时滞后可能不存在（模型吐得少），那就只验信封，不硬凑断言。
    """
    h = Harness(workdir / "log")
    seen: dict[str, int] = {}
    h.bus.subscribe(slow_observer(0.02, session="A"))
    h.bus.subscribe(counter(seen, session="A"))
    observed_at_end = [0]

    async def go() -> Event:
        h.send("保温杯还有库存吗")
        end = await h.wait("turn_end")
        observed_at_end[0] = seen.get("total", 0)
        await h.bus.drain(timeout=30.0)
        await h.stop()
        return end

    end = asyncio.run(go())
    assert end.seq > 0
    assert end.correlation_id.startswith("turn-")

    records = EventLog(str(workdir / "log")).read_since(0)
    cluster = [r for r in records if r["corr"] == end.correlation_id]
    seqs = [r["seq"] for r in cluster]
    assert seqs == sorted(seqs) and len(cluster) >= 3
    assert seen.get("turn_end") == 1  # 生命周期事件一条没丢
    if len(cluster) >= 30:
        # 慢观测者没追上 = 分发确实没在 loop 里等订阅者
        assert observed_at_end[0] < len(cluster)


def test_interrupt_semantics_survive_the_transport_rewrite(workdir: Path) -> None:
    """回归：换了传输层，stage03 的“掐掉在飞的一步、turn 以 interrupted 收尾”照旧。"""
    h = Harness(workdir / "log")

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
    # 合成消息也进了事实层（否则回放重建不出这段 history）
    records = EventLog(str(workdir / "log")).read_since(0)
    assert [r for r in records if r["payload"].get("synthetic")]
