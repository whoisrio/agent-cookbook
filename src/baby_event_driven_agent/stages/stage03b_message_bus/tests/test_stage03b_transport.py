"""Stage 4 传输层用例：全部离线（不打模型）。

要验的是“事件怎么到达消费者”这一层，跟模型无关，所以用脚本化 LLM
（ScriptedLLM）把工具调用变成确定性事件，其余用例直接打总线 / 缓冲。

覆盖：
- 投递契约：StreamConsumer 的 offer 微秒级只做缓冲，渲染挪到帧界回调；
  await 型 handler 直接被 emit 消费（契约：微秒级只做接收）
- 洪峰下全部送达：总线零策略、零丢弃，节奏归消费者
- 订阅约束：stream 事件只允许邮箱型消费者——约束由事件类型声明，
  总线在订阅进门时问它（构造 Subscription 不校验）
- 治理：否决 → 工具不执行、事件不投递；改写 → 按改过的参数执行
- 合并缓冲（CoalescingBuffer）：满了立刻刷 / 到帧界刷，二者其一
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from baby_event_driven_agent.stages.stage03b_message_bus.agent import BLOCKED_PREFIX, Agent
from baby_event_driven_agent.stages.stage03b_message_bus.bus import EventBus
from baby_event_driven_agent.stages.stage03b_message_bus.events import (
    MODIFY,
    Decision,
    Event,
    Subscription,
    UserMessage,
)
from baby_event_driven_agent.stages.stage03b_message_bus.governance import Rule
from baby_event_driven_agent.stages.stage03b_message_bus.tools import TOOLS
from baby_event_driven_agent.stages.stage03b_message_bus.outbound import (
    CoalescingBuffer,
    StreamConsumer,
)
from baby_event_driven_agent.stages.stage03b_message_bus.subscribers import (
    counter,
    permission_guard,
)


class RecordingStream(StreamConsumer):
    """测试用 stream 消费者：记录每条事件 + 合并缓冲计数。"""

    def __init__(self) -> None:
        super().__init__(max_chars=96, frame=0.05)
        self.events: list[Event] = []

    def offer(self, event: Event) -> None:
        self.events.append(event)
        super().offer(event)

    def on_flush(self, text: str) -> None:
        pass


@pytest.fixture()
def inventory_spy() -> list[dict[str, Any]]:
    """把 update_inventory 换成只记录不落盘的探针：断言“有没有执行”就够了，
    不要真去改仓库里的知识库文件。"""
    calls: list[dict[str, Any]] = []
    original = TOOLS["update_inventory"]

    async def spy(args: dict[str, Any]) -> str:
        calls.append(args)
        return f"已更新：{args.get('category')}：库存 {args.get('stock')} 件"

    TOOLS["update_inventory"] = replace(original, fn=spy, requires_approval=False)
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


# ------------------------------------------------------------------ 投递契约


def test_stream_consumer_keeps_emit_fast() -> None:
    """热路径契约：渲染（慢活）在 on_flush 按帧结算，emit 保持微秒级。"""
    renders: list[str] = []

    class SlowRender(StreamConsumer):
        def on_flush(self, text: str) -> None:
            import time

            time.sleep(0.005)  # 渲染一帧 5ms
            renders.append(text)

    bus = EventBus()
    consumer = SlowRender()
    bus.subscribe(Subscription("ui", ("agent_delta",), consumer))

    async def go() -> float:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for _ in range(200):
            await bus.emit(Event("agent_delta", "S", {"text": "字"}))
        cost = loop.time() - t0
        await consumer.stop()
        return cost

    cost = run(go())
    # 违约写法（逐事件渲染 5ms）要 1s；缓冲后渲染按帧结算（200 字 / 96 字 ≈ 3 帧）
    assert cost < 0.2, cost
    assert 1 <= len(renders) <= 5



def test_flood_all_delivered_merged() -> None:
    """洪峰：2000 个 delta 全部送达（零丢弃），UI 自己合并刷帧；
    生命周期事件同轮即时送达。"""
    seen: dict[str, int] = {}
    bus = EventBus()
    consumer = RecordingStream()
    bus.subscribe(Subscription("ui", ("agent_delta",), consumer))
    bus.subscribe(counter(seen, session="F"))

    async def go() -> int:
        for _ in range(2000):
            await bus.emit(Event("agent_delta", "F", {"text": "字"}))
        await bus.emit(Event("turn_end", "F", {"reason": "flood"}))
        return len(consumer.events)

    received = run(go())
    assert received == 2000  # 全部送达：总线零丢弃
    assert seen.get("turn_end") == 1  # 生命周期事件同轮即时送达
    assert consumer.flushes < 40  # 合并刷屏：2000 字 / 96 字每帧 ≈ 21 帧




# ------------------------------------------------------------------ 示例订阅者



# ------------------------------------------------------------------ 治理


def test_governance_deny_blocks_tool(inventory_spy: list[dict[str, Any]]) -> None:
    """被否决的工具不执行：上下文里是一条自描述占位，裁决由 Verdict 带回 agent。"""
    bus = EventBus()
    agent = Agent(bus, ScriptedLLM([CALL_INVENTORY, FINAL_TEXT]))
    agent.governor.add(permission_guard("update_inventory"))

    async def go() -> None:
        done = asyncio.Event()

        async def on_end(event: Event) -> None:
            done.set()

        bus.subscribe(Subscription("end", ("turn_end",), on_end))
        bus.publish(
            UserMessage("user_input", "A", {"text": "把保温杯库存改成 42 件"}), to=agent.agent_id
        )
        await done.wait()
        await agent.stop()

    run(go())
    assert inventory_spy == []  # 工具一次都没执行
    placeholder = [
        m for m in agent.history["A"] if str(m.get("content", "")).startswith(BLOCKED_PREFIX)
    ]
    assert placeholder, agent.history["A"]


def test_governance_modify_rewrites_arguments(
    inventory_spy: list[dict[str, Any]],
) -> None:
    """治理还能改写：规则链的 Verdict 说了算，工具按改过的参数执行。"""
    async def rewrite(name: str, arguments: str) -> Decision:
        return Decision(
            action=MODIFY,
            by="rewriter",
            reason="品类改名",
            patch={"arguments": '{"category": "玻璃杯", "stock": 7}'},
        )

    bus = EventBus()
    agent = Agent(bus, ScriptedLLM([CALL_INVENTORY, FINAL_TEXT]))
    agent.governor.add(Rule(name="rewriter", handler=rewrite, order=5))

    async def go() -> None:
        done = asyncio.Event()

        async def on_end(event: Event) -> None:
            done.set()

        bus.subscribe(Subscription("end", ("turn_end",), on_end))
        bus.publish(
            UserMessage("user_input", "A", {"text": "把保温杯库存改成 42 件"}), to=agent.agent_id
        )
        await done.wait()
        await agent.stop()

    run(go())
    assert inventory_spy == [{"category": "玻璃杯", "stock": 7}]


# ------------------------------------------------------------------ 合并缓冲


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


def test_buffer_turns_800_deltas_into_a_few_frames() -> None:
    """合并缓冲的可视效果：800 个增量全部送达，上屏从 800 帧降到十来帧。

    合并发生在消费者的热路径之外：offer 逐条缓冲（微秒级），
    on_flush 按帧结算（800 字 / 96 字每帧 ≈ 9 帧）。
    """
    frames: list[str] = []
    consumer = RecordingStream()

    async def go() -> int:
        bus = EventBus()
        bus.subscribe(Subscription("ui", ("agent_delta",), consumer))
        for _ in range(800):
            await bus.emit(Event("agent_delta", "G", {"text": "字"}))
        await consumer.stop()
        return len(consumer.events)

    received = run(go())
    assert received == 800  # 全部送达：一条没丢
    assert consumer.flushes < 40  # 合并刷屏：800 字 / 96 字每帧 ≈ 9 帧


def test_flood_without_yields_still_delivers() -> None:
    """直接分派没有队列：不让出也一条不丢——投递发生在 emit 内部，无积压可言。"""
    consumer = RecordingStream()
    bus = EventBus()
    bus.subscribe(Subscription("ui", ("agent_delta",), consumer))

    async def go() -> int:
        for _ in range(800):
            await bus.emit(Event("agent_delta", "S", {"text": "字"}))
        return len(consumer.events)

    received = run(go())
    assert received == 800  # 没有队列就没有积压：全部即时送达


def test_mailbox_only_checked_at_subscribe_not_at_construction() -> None:
    """Subscription 是纯数据：构造不校验；约束在订阅进门时由总线问事件本身。"""

    async def await_handler(event: Event) -> None:
        pass  # await 型：不符合 stream 事件的消费约束

    # 构造随便是合法的——订阅者对约束一无所知
    sub = Subscription("bad", ("agent_delta",), await_handler)

    bus = EventBus()
    with pytest.raises(ValueError, match="只允许 Mailbox 型消费者"):
        bus.subscribe(sub)  # 进门才问，答不上来就不登记
    assert bus._subs == []  # 拒之门外，订阅列表干净
