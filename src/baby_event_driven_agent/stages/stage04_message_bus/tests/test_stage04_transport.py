"""Stage 4 传输层用例：全部离线（不打模型）。

要验的是“事件怎么到达消费者”这一层，跟模型无关，所以用脚本化 LLM
（ScriptedLLM）把工具调用变成确定性事件，其余用例直接打总线 / 落盘 / 缓冲。

覆盖：
- 慢消费者不拖生产者（异步分发）
- 洪峰下 stream 道丢自己的、state 道一条不丢（QoS）
- 信封：seq 单调、落盘即编号、重启续号；correlation_id 把一轮聚成一簇
- 治理：否决 → 工具不执行；改写 → 按改过的参数执行
- 落盘：长度前缀 + CRC 判定残尾；位点续读；脱敏只改落盘那份；保留按段滚动
- 背压合并缓冲：满了立刻刷 / 到帧界刷，二者其一
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from baby_event_driven_agent.stages.stage04_message_bus.agent import BLOCKED_PREFIX, Agent
from baby_event_driven_agent.stages.stage04_message_bus.bus import EventBus
from baby_event_driven_agent.stages.stage04_message_bus.events import (
    MODIFY,
    OBSERVE,
    Decision,
    Event,
    Subscription,
)
from baby_event_driven_agent.stages.stage04_message_bus.llm import TOOLS
from baby_event_driven_agent.stages.stage04_message_bus.outbound import CoalescingBuffer
from baby_event_driven_agent.stages.stage04_message_bus.persistence import EventLog
from baby_event_driven_agent.stages.stage04_message_bus.subscribers import (
    counter,
    permission_guard,
    slow_observer,
)


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage04_transport_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def inventory_spy() -> list[dict[str, Any]]:
    """把 update_inventory 换成只记录不落盘的探针：断言“有没有执行”就够了，
    不要真去改仓库里的知识库文件。"""
    calls: list[dict[str, Any]] = []
    original = TOOLS["update_inventory"]

    async def spy(args: dict[str, Any]) -> str:
        calls.append(args)
        return f"已更新：{args.get('category')}：库存 {args.get('stock')} 件"

    TOOLS["update_inventory"] = spy
    yield calls
    TOOLS["update_inventory"] = original


class ScriptedLLM:
    """脚本化 LLM：每个请求吐一段固定增量。治理用例要的是“一定会调那个工具”。"""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls = 0

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for chunk in self.script[idx]:
            yield chunk


CALL_INVENTORY = [
    {
        "type": "tool_call_delta",
        "index": 0,
        "id": "call_1",
        "name": "update_inventory",
        "args_delta": '{"category": "保温杯", "stock": 42}',
    }
]
FINAL_TEXT = [{"type": "text_delta", "text": "已记录。"}]

TIMEOUT = 10.0


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT))


# ------------------------------------------------------------------ 异步分发


def test_slow_consumer_does_not_block_emitter(workdir: Path) -> None:
    """慢订阅者（10ms/事件）只该拖慢自己那条道，不该拖慢 emit。"""
    seen: dict[str, int] = {}
    log = EventLog(str(workdir / "log"))
    bus = EventBus(log)
    bus.subscribe(slow_observer(0.01, session="S"))
    bus.subscribe(counter(seen, session="S"))

    async def go() -> float:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for _ in range(60):
            await bus.emit(Event("agent_delta", "S", {"text": "字"}))
        cost = loop.time() - t0
        await bus.drain(timeout=30.0)
        return cost

    cost = run(go())
    # 同步扇出要 60 × 10ms = 0.6s；异步分发只花的是入队 + 落盘
    assert cost < 0.2, cost
    assert seen.get("total") == 60  # 慢归慢，一条没丢


def test_flood_drops_stream_but_never_state(workdir: Path) -> None:
    """洪峰：token 增量丢自己的，生命周期事件一条不丢。"""
    seen: dict[str, int] = {}
    bus = EventBus(EventLog(str(workdir / "log")), stream_size=8)
    bus.subscribe(slow_observer(0.002, session="F", name="flood"))
    bus.subscribe(counter(seen, session="F"))

    async def go() -> dict:
        for _ in range(300):
            await bus.emit(Event("agent_delta", "F", {"text": "字"}))
        await bus.emit(Event("turn_end", "F", {"reason": "flood"}))
        await bus.drain(timeout=30.0)
        return bus.stats()

    stats = run(go())
    assert stats["lanes"]["stream"]["dropped"] > 0  # 可丢的丢了
    assert stats["lanes"]["state"]["dropped"] == 0
    assert seen.get("turn_end") == 1  # 不可丢的一条没丢


# ------------------------------------------------------------------ 信封


def test_seq_is_monotonic_and_survives_reopen(workdir: Path) -> None:
    """seq 落盘即编号：进程内单调，重启（重新打开目录）接着走。"""
    log = EventLog(str(workdir / "log"))
    bus = EventBus(log)

    async def go() -> list[int]:
        seqs = []
        for _ in range(3):
            seqs.append((await bus.emit(Event("tick", "S", {}))).event.seq)
        return seqs

    assert run(go()) == [1, 2, 3]
    reopened = EventLog(str(workdir / "log"))  # 模拟重启：索引从段文件重建
    assert reopened.last_seq == 3
    assert [r["seq"] for r in reopened.read_since(1)] == [2, 3]


def test_correlation_id_clusters_one_turn(workdir: Path, inventory_spy: list[dict]) -> None:
    """correlation_id 把一次 turn 的所有事件聚成一簇。"""
    bus = EventBus(EventLog(str(workdir / "log")))
    agent = Agent(bus, ScriptedLLM([CALL_INVENTORY, FINAL_TEXT]))

    async def go() -> None:
        done = asyncio.Event()

        async def on_end(event: Event) -> None:
            done.set()

        bus.subscribe(Subscription("end", ("turn_end",), on_end, mode=OBSERVE))
        bus.publish(
            Event("user_input", "A", {"text": "把保温杯库存改成 42 件"}), to=agent.agent_id
        )
        await done.wait()
        await bus.drain(timeout=10.0)
        await agent.stop()

    run(go())
    records = EventLog(str(workdir / "log")).read_since(0)
    corrs = {r["corr"] for r in records}
    assert len(corrs) == 1, corrs  # 一轮一簇
    assert next(iter(corrs)).startswith("turn-")
    assert [r["seq"] for r in records] == sorted(r["seq"] for r in records)


# ------------------------------------------------------------------ 治理


def test_governance_deny_blocks_tool(
    workdir: Path, inventory_spy: list[dict[str, Any]]
) -> None:
    """被否决的工具不执行：上下文里是一条自描述占位，裁决进 log。"""
    bus = EventBus(EventLog(str(workdir / "log")))
    bus.subscribe(permission_guard("update_inventory"))
    agent = Agent(bus, ScriptedLLM([CALL_INVENTORY, FINAL_TEXT]))

    async def go() -> None:
        done = asyncio.Event()

        async def on_end(event: Event) -> None:
            done.set()

        bus.subscribe(Subscription("end", ("turn_end",), on_end, mode=OBSERVE))
        bus.publish(
            Event("user_input", "A", {"text": "把保温杯库存改成 42 件"}), to=agent.agent_id
        )
        await done.wait()
        await bus.drain(timeout=10.0)
        await agent.stop()

    run(go())
    assert inventory_spy == []  # 工具一次都没执行
    placeholder = [
        m for m in agent.history["A"] if str(m.get("content", "")).startswith(BLOCKED_PREFIX)
    ]
    assert placeholder, agent.history["A"]
    decisions = [
        d
        for r in EventLog(str(workdir / "log")).read_since(0)
        for d in r.get("decisions", [])
        if d["action"] == "deny"
    ]
    assert decisions and decisions[0]["by"] == "permission_guard"


def test_governance_modify_rewrites_arguments(
    workdir: Path, inventory_spy: list[dict[str, Any]]
) -> None:
    """治理还能改写：emit 返回的事件说了算，工具按改过的参数执行。"""
    async def rewrite(event: Event) -> Decision:
        return Decision(
            action=MODIFY,
            by="rewriter",
            reason="品类改名",
            patch={"arguments": '{"category": "玻璃杯", "stock": 7}'},
        )

    bus = EventBus(EventLog(str(workdir / "log")))
    bus.subscribe(
        Subscription("rewriter", ("before_tool_call",), rewrite, mode="intercept", order=5)
    )
    agent = Agent(bus, ScriptedLLM([CALL_INVENTORY, FINAL_TEXT]))

    async def go() -> None:
        done = asyncio.Event()

        async def on_end(event: Event) -> None:
            done.set()

        bus.subscribe(Subscription("end", ("turn_end",), on_end, mode=OBSERVE))
        bus.publish(
            Event("user_input", "A", {"text": "把保温杯库存改成 42 件"}), to=agent.agent_id
        )
        await done.wait()
        await bus.drain(timeout=10.0)
        await agent.stop()

    run(go())
    assert inventory_spy == [{"category": "玻璃杯", "stock": 7}]


# ------------------------------------------------------------------ 落盘


def test_torn_tail_stops_before_bad_record(workdir: Path) -> None:
    """崩在写一半：长度/CRC 判定残尾，读到坏记录为止，前面一条不少。"""
    log = EventLog(str(workdir / "log"))
    bus = EventBus(log)

    async def go() -> None:
        for _ in range(4):
            await bus.emit(Event("tick", "S", {"i": 1}))

    run(go())
    seg = sorted((workdir / "log").glob("evt-*.log"))[-1]
    raw = seg.read_bytes()
    seg.write_bytes(raw[:-11])  # 砍掉最后 11 字节：崩在写一半
    got = EventLog(str(workdir / "log")).read_since(0)
    assert [r["seq"] for r in got] == [1, 2, 3]


def test_offset_resume(workdir: Path) -> None:
    """位点：已消费到最后一条 seq，重启从下一条续读。"""
    log = EventLog(str(workdir / "log"))
    bus = EventBus(log)

    async def go() -> None:
        for _ in range(10):
            await bus.emit(Event("tick", "S", {}))

    run(go())
    log.commit("ui", 4)
    reopened = EventLog(str(workdir / "log"))
    assert reopened.offset("ui") == 4
    assert [r["seq"] for r in reopened.read_since(reopened.offset("ui"))] == list(range(5, 11))


def test_redaction_only_touches_disk(workdir: Path) -> None:
    """脱敏只改落盘那份：内存里的事件不变（治理看到的和落下的要能对账）。"""
    log = EventLog(str(workdir / "log"))
    bus = EventBus(log)

    async def go():
        return await bus.emit(
            Event("tick", "S", {"api_key": "sk-secret", "question": "报销"})
        )

    result = run(go())
    assert result.event.payload["api_key"] == "sk-secret"  # 内存里没动
    on_disk = EventLog(str(workdir / "log")).read_since(0)[0]["payload"]
    assert on_disk["api_key"] == "***"
    assert on_disk["question"] == "报销"


def test_retention_rolls_old_segments(workdir: Path) -> None:
    """保留：按段滚动，删掉最老的一段，位点因此可能落在现存最老之后。"""
    log = EventLog(str(workdir / "log"), segment_bytes=150, keep_segments=2)
    bus = EventBus(log)

    async def go() -> None:
        for _ in range(60):
            await bus.emit(Event("tick", "S", {"i": 1}))

    run(go())
    stats = log.stats()
    assert stats["segments"] <= 2
    assert stats["oldest_seq"] > 1
    assert len(log.read_since(0)) < 60  # 老段被删了，读不回来


# ------------------------------------------------------------------ 背压合并缓冲


def test_buffer_flushes_on_full_or_frame() -> None:
    """二者其一：满了立刻刷；不满就等帧界。只等满会让 UI 一顿一顿。"""
    out: list[str] = []

    async def go() -> CoalescingBuffer:
        buf = CoalescingBuffer(out.append, max_chars=10, frame=0.05)
        await buf.start()
        buf.add("abc")
        assert out == []  # 没到帧界，也没满
        await asyncio.sleep(0.09)
        assert out == ["abc"]  # 帧界到了 → 刷
        buf.add("0123456789")  # 满 10 字 → 立刻刷，不等帧界
        assert out == ["abc", "0123456789"]
        await buf.stop()
        return buf

    buf = asyncio.run(asyncio.wait_for(go(), TIMEOUT))
    assert buf.flushes == 2
    assert buf.merged == 2
