"""Stage 4 传输层用例：全部离线（不打模型）。

要验的是“事件怎么到达消费者”这一层，跟模型无关，所以用脚本化 LLM
（ScriptedLLM）把工具调用变成确定性事件，其余用例直接打总线 / 落盘 / 缓冲。

覆盖：
- 慢消费者不拖生产者（异步分发）
- 洪峰下 stream 道丢自己的、state 道一条不丢（QoS）
- 信封：seq 单调、落盘即编号、重启续号；correlation_id 把一轮聚成一簇；
  seq = 落盘顺序 ≠ emit 调用顺序（并发下的倒挂是声明过的行为，会话内因果不破）
- 治理：否决 → 工具不执行；改写 → 按改过的参数执行
- 落盘：长度前缀 + CRC 判定残尾；位点续读；脱敏只改落盘那份；保留按段滚动
- 背压合并缓冲：满了立刻刷 / 到帧界刷，二者其一
- 道隔离：state 道满堵住生产者时，stream 道照常投递（两道间无共享投递段）
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
    arrival_probe,
    counter,
    latency_probe,
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


def test_state_backpressure_does_not_block_stream_lane(workdir: Path) -> None:
    """道隔离：state 道满、生产者挂起时，stream 道照常投递。

    背压的等待发生在"调用这次 emit 的那个协程"上——隔离单位是 task：
    两条道是独立的队列 + 独立的 worker，之间没有任何同步点。
    （代价是同 session 的 loop 会被连坐拖慢——那是背压的语义，不是 bug。）
    """

    async def go() -> dict:
        seen: dict[str, int] = {}
        bus = EventBus(EventLog(str(workdir / "log")), state_size=2)
        bus.subscribe(counter(seen, session="S"))

        release = asyncio.Event()
        worker_stuck = asyncio.Event()

        async def gate(event: Event) -> None:
            if event.type == "turn_end":
                worker_stuck.set()
                await release.wait()  # 挂住 state worker：队列从此消化不动

        bus.subscribe(Subscription("gate", ("turn_end",), gate, mode=OBSERVE))

        # 第 1 条被 worker 取走，卡在 gate 里；第 2、3 条填满队列；第 4 条 put 挂起
        await bus.emit(Event("turn_end", "S", {"i": 0}))
        await worker_stuck.wait()
        await bus.emit(Event("turn_end", "S", {"i": 1}))
        await bus.emit(Event("turn_end", "S", {"i": 2}))
        task = asyncio.create_task(bus.emit(Event("turn_end", "S", {"i": 3})))
        await asyncio.sleep(0.02)
        assert not task.done()  # 生产者被背压挂住
        assert bus.stats()["lanes"]["state"]["queued"] == 2  # 队列满

        # state 道堵死，stream 道照常进、照常投
        await bus.emit(Event("agent_delta", "S", {"text": "字"}))
        await asyncio.sleep(0.05)
        assert seen.get("agent_delta") == 1  # stream 道已经送达，没被连坐

        release.set()  # 放行 state worker，一切消化完
        await task
        await bus.drain(timeout=5.0)
        return bus.stats()

    stats = run(go())
    assert stats["lanes"]["state"]["delivered"] == 4  # 挂起的那条最终也进来了
    assert stats["lanes"]["state"]["dropped"] == 0
    assert stats["lanes"]["stream"]["delivered"] == 1


# ------------------------------------------------------------------ 示例订阅者


def test_probe_subscribers_record_lags_and_arrivals(workdir: Path) -> None:
    """latency_probe / arrival_probe：量送达延迟、记送达时刻与当时的水位。

    观察与呈现分离：探针只记账（on_arrival 同步回调），打印是调用方的事。
    """
    lags: list[float] = []
    records: list[dict[str, Any]] = []
    seen_arrivals: list[str] = []
    bus = EventBus(EventLog(str(workdir / "log")))
    bus.subscribe(latency_probe(lags, session="P"))
    pos = [7]
    bus.subscribe(
        arrival_probe(
            records,
            session="P",
            types=("turn_end",),
            progress=pos,
            snapshot=bus.stats,
            on_arrival=lambda r: seen_arrivals.append(r["type"]),
        )
    )

    async def go() -> None:
        await bus.emit(Event("agent_delta", "P", {"text": "字"}))  # 计入 lags
        await bus.emit(Event("tick", "P", {}))  # 两个探针都不收
        await bus.emit(Event("turn_end", "P", {}))  # 计入 records
        await bus.drain(timeout=5.0)

    run(go())
    assert len(lags) == 1 and lags[0] >= 0
    assert len(records) == 1
    assert records[0]["type"] == "turn_end"
    assert records[0]["at"] == 7  # progress 原样入账
    assert records[0]["lanes"]["state"]["delivered"] == 1  # snapshot 是送达后的水位
    assert seen_arrivals == ["turn_end"]  # 同步回调在送达当场被调


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


def test_seq_is_persist_order_not_call_order(workdir: Path) -> None:
    """seq = 落盘顺序，不是 emit 调用顺序（信封语义的精确化）。

    A 的 gated 事件先调用、但卡在拦截者上；B 的事件不匹配拦截者，同步落盘
    先拿号。钉住三件事：全局 seq 仍严格单调；同一 session 的 emit 是 await
    串行，按 session 过滤后 seq = 事件发生顺序；seq 与 ts（构造时刻）可以
    逆序——回放排序一律用 seq，不用 ts。
    """

    async def slow_allow(event: Event) -> Decision:
        await asyncio.sleep(0.05)
        return Decision.allow("slow_governor")

    bus = EventBus(EventLog(str(workdir / "log")))
    bus.subscribe(
        Subscription("slow_governor", ("gated",), slow_allow, mode="intercept")
    )

    async def go() -> tuple[float, float, list[int]]:
        ev_a1 = Event("gated", "A", {"n": 1})
        task_a = asyncio.create_task(bus.emit(ev_a1))
        await asyncio.sleep(0.01)  # A 已经在拦截者的 await 里睡着
        ev_b1 = Event("tick", "B", {"i": 0})
        seq_b1 = (await bus.emit(ev_b1)).event.seq
        seq_b2 = (await bus.emit(Event("tick", "B", {"i": 1}))).event.seq
        seq_a1 = (await task_a).event.seq
        seq_a2 = (await bus.emit(Event("gated", "A", {"n": 2}))).event.seq
        return ev_a1.ts, ev_b1.ts, [seq_b1, seq_b2, seq_a1, seq_a2]

    ts_a, ts_b, seqs = run(go())
    # A 先调用（也先构造），却后落盘——调用顺序 ≠ seq 顺序，这是声明过的行为
    assert seqs == [1, 2, 3, 4]
    assert ts_a < ts_b
    records = EventLog(str(workdir / "log")).read_since(0)
    global_seqs = [r["seq"] for r in records]
    assert global_seqs == sorted(global_seqs) == [1, 2, 3, 4]  # 全局严格单调
    by_session: dict[str, list[int]] = {}
    for r in records:
        by_session.setdefault(r["session"], []).append(r["seq"])
    assert by_session["A"] == [3, 4]  # 会话内：seq = 事件发生顺序，因果不破
    assert by_session["B"] == [1, 2]


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


def test_buffer_turns_800_deltas_into_a_few_frames(workdir: Path) -> None:
    """合并缓冲的可视效果：800 个增量一条不丢，上屏从 800 帧降到十来帧。

    "没丢"的前提是生产者让出（每 10 条 yield 一次，worker 每次都能清空积压）；
    缓冲买到的是 IO：逐条上屏 800 次 vs 合并后 ≪800。
    """
    frames: list[str] = []

    async def go() -> tuple[int, int]:
        seen = 0
        bus = EventBus(EventLog(str(workdir / "log")))
        buf = CoalescingBuffer(frames.append, max_chars=96, frame=0.05)
        await buf.start()

        async def ui(event: Event) -> None:
            nonlocal seen
            seen += 1
            buf.add(str(event.payload.get("text", "")))

        bus.subscribe(Subscription("ui", ("agent_delta",), ui, mode=OBSERVE))
        for i in range(800):
            await bus.emit(Event("agent_delta", "G", {"text": "字"}))
            if i % 10 == 0:
                await asyncio.sleep(0)
        await bus.drain(timeout=30.0)
        await buf.stop()
        return seen, buf.flushes

    seen, flushes = run(go())
    assert seen == 800  # 快消费者 + 让出点：一条没丢
    assert flushes < 40  # 逐条上屏是 800 帧，这里是十来帧


def test_flood_without_yields_drops_beyond_queue_size(workdir: Path) -> None:
    """没有让出点，快消费者也救不了：800 条一个调度片灌完，worker 一条没捞到，
    队列（64）之外的 736 条全丢——"不丢"的前提是生产者让出，不是消费者快。"""
    seen: dict[str, int] = {}
    bus = EventBus(EventLog(str(workdir / "log")))
    bus.subscribe(counter(seen, session="S"))

    async def go() -> dict:
        for _ in range(800):  # 不让出：emit 全程无挂起点（见 flood 的 docstring）
            await bus.emit(Event("agent_delta", "S", {"text": "字"}))
        await bus.drain(timeout=30.0)
        return bus.stats()

    stats = run(go())
    assert stats["lanes"]["stream"]["delivered"] == 64  # 只有队列里那 64 条
    assert stats["lanes"]["stream"]["dropped"] == 736
    assert seen.get("agent_delta") == 64
