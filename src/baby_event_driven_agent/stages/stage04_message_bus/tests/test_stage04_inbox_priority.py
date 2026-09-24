"""Stage 4 下行收件箱用例：意图（followup / steering）+ 优先级，全部离线。

要验的是这些边界：
- `followup`（默认）：turn 在飞时发的普通消息**只排队、不插话**，turn 结束后
  按优先级作为新 turn 处理（同级 FIFO）；
- `steering`：turn 在飞时在 step 边界按优先级 drain 进当前上下文；**不改变
  turn 的边界**（不新开 turn）；
- 降级：turn 空闲时被取到的 steering 自然变成主输入（无需特殊代码）；
- 打断（stop）优先于一切队列：turn 按 interrupted 收尾，worker 立刻消费
  followup inbox 里优先级最高的一条；
- 转向（redirect）的纠正先落地，steering inbox 里的插话按优先级跟在后面。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from baby_event_driven_agent.stages.stage04_message_bus.agent import STOP_CLOSER, Agent
from baby_event_driven_agent.stages.stage04_message_bus.bus import EventBus
from baby_event_driven_agent.stages.stage04_message_bus.events import (
    STEERING,
    Event,
    Subscription,
    UserMessage,
)
from baby_event_driven_agent.stages.stage04_message_bus.tools import TOOLS
from baby_event_driven_agent.stages.stage04_message_bus.outbound import StreamConsumer

TIMEOUT = 10.0
SID = "A"

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


class ScriptedLLM:
    """脚本化 LLM：脚本循环使用（超出长度后重复最后一段）。

    chunk 支持 {"sleep": 秒} 伪指令——在流中间挂起，制造"最后的 drain 已过、
    turn 还没结束"的插话窗口。
    """

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls = 0

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for chunk in self.script[idx]:
            if "sleep" in chunk:
                await asyncio.sleep(float(chunk["sleep"]))
                continue
            yield chunk


class Harness:
    """bus + agent + 记录器。user_input / turn_end / steering / tool 可观察。"""

    def __init__(self, script: list[list[dict[str, Any]]] | None = None) -> None:
        self.bus = EventBus()
        self.agent = Agent(self.bus, ScriptedLLM(script or [CALL_INVENTORY, FINAL_TEXT]))
        self.turn_ends: list[Event] = []
        self.steerings: list[Event] = []
        self.inputs: list[str] = []  # 每个 turn 的主输入文本（按处理顺序）
        self.turn_end = asyncio.Event()
        self.steer_seen = asyncio.Event()
        self.tool_started = asyncio.Event()
        self.bus.subscribe(
            Subscription("rec-end", ("turn_end",), self._record_end)
        )
        self.bus.subscribe(
            Subscription(
                "rec-steer", ("steering_consumed",), self._record_steering
            )
        )
        self.bus.subscribe(
            Subscription(
                "rec-tool", ("tool_call_started",), self._record_tool
            )
        )
        self.bus.subscribe(
            Subscription("rec-input", ("user_input",), self._record_input)
        )

    async def _record_end(self, event: Event) -> None:
        self.turn_ends.append(event)
        self.turn_end.set()

    async def _record_steering(self, event: Event) -> None:
        self.steerings.append(event)
        self.steer_seen.set()

    async def _record_tool(self, event: Event) -> None:
        self.tool_started.set()

    async def _record_input(self, event: Event) -> None:
        self.inputs.append(str(event.payload["text"]))

    def send(
        self,
        text: str,
        *,
        intent: str = "followup",
        priority: int = 100,
        sid: str = SID,
    ) -> None:
        self.bus.publish(
            UserMessage("user_input", sid, {"text": text}, intent=intent, priority=priority),
            to=self.agent.agent_id,
        )

    def interrupt(self, sid: str = SID) -> None:
        self.bus.publish(
            Event("user_interrupt", sid, {"intent": "stop"}), to=self.agent.agent_id
        )

    def redirect(self, text: str, sid: str = SID) -> None:
        self.bus.publish(
            Event(
                "user_interrupt", sid, {"intent": "redirect", "text": text}
            ),
            to=self.agent.agent_id,
        )

    async def stop(self) -> None:
        await self.agent.stop()

    @property
    def history_texts(self) -> list[str]:
        return [str(m.get("content", "")) for m in self.agent.history[SID]]


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT))


async def wait_turns(h: Harness, n: int) -> None:
    """脚本化 turn 转得飞快，轮询计数比逐个等事件稳。"""
    while len(h.turn_ends) < n:
        await asyncio.sleep(0.02)


@pytest.fixture()
def slow_update_tool():
    """把写工具换成慢探针：制造"turn 正在飞"的窗口，且不碰仓库文件。"""
    original = TOOLS["update_inventory"]

    async def slow_spy(args: dict[str, Any]) -> str:
        await asyncio.sleep(0.4)
        return "已更新"

    TOOLS["update_inventory"] = replace(original, fn=slow_spy, requires_approval=False)
    yield
    TOOLS["update_inventory"] = original


def test_followup_default_queues_never_steers() -> None:
    """默认意图是 followup：turn 在飞时发的消息只排队，不插话。

    四条消息（优先级乱序）全部等当前 turn 结束后才被消费，按优先级开新轮，
    同优先级 FIFO；全程没有一次 steering drain。
    """

    async def go() -> tuple[list[str], list[Event]]:
        h = Harness(script=[FINAL_TEXT])  # 纯文本 turn，不触发任何工具
        h.send("常规一", priority=100)
        h.send("加急", priority=10)
        h.send("常规二", priority=100)
        h.send("次急", priority=50)
        await wait_turns(h, 4)
        await h.stop()
        return list(h.inputs), list(h.steerings)

    order, steerings = run(go())
    assert order == ["加急", "次急", "常规一", "常规二"]  # 优先级序 + 同级 FIFO
    assert steerings == []  # 默认意图不产生插话


def test_steering_drained_by_priority(slow_update_tool) -> None:
    """steering：turn 在飞时点名的插话，step 边界按优先级拼进当前上下文。"""

    async def go() -> tuple[list[str], int]:
        h = Harness()
        h.send("先改库存")
        await asyncio.wait_for(h.tool_started.wait(), TIMEOUT)
        # 工具在飞：连投两条插话，优先级故意乱序
        h.send("常规插话", intent=STEERING, priority=100)
        h.send("加急插话", intent=STEERING, priority=10)
        await asyncio.wait_for(h.steer_seen.wait(), TIMEOUT)
        await h.turn_end.wait()
        consumed = [list(ev.payload["texts"]) for ev in h.steerings]
        await h.stop()
        return consumed, len(h.turn_ends)

    consumed, turns = run(go())
    assert consumed == [["加急插话", "常规插话"]]  # 一次 drain，按优先级
    assert turns == 1  # 插话不改变 turn 边界


def test_followup_and_steering_do_not_mix(slow_update_tool) -> None:
    """两类消息同时在飞：steering 进当前轮，followup 留到后面的轮。"""

    async def go() -> tuple[list[str], list[Event]]:
        h = Harness()
        h.send("先改库存")
        await asyncio.wait_for(h.tool_started.wait(), TIMEOUT)
        h.send("排队的话", priority=100)  # 默认 followup：不插话
        h.send("插进本轮", intent=STEERING, priority=50)
        await asyncio.wait_for(h.steer_seen.wait(), TIMEOUT)
        await wait_turns(h, 2)  # 本轮 + followup 的新轮
        await h.stop()
        return list(h.inputs), list(h.steerings)

    order, steerings = run(go())
    assert steerings[0].payload["texts"] == ["插进本轮"]  # followup 没被 drain
    assert order == ["先改库存", "排队的话"]  # followup 作为新 turn 的主输入


def test_idle_steering_degrades_to_main_input() -> None:
    """降级：worker 空闲时取到 steering，无可插对象 → 自然变成主输入。"""

    async def go() -> tuple[list[str], list[Event]]:
        h = Harness(script=[FINAL_TEXT])
        h.send("点名插话", intent=STEERING, priority=10)
        await wait_turns(h, 1)
        await h.stop()
        return list(h.inputs), list(h.steerings)

    order, steerings = run(go())
    assert order == ["点名插话"]  # 没有可插的 turn，降级成主输入
    assert steerings == []  # 没发生过 drain


def test_interrupt_consumes_next_followup_by_priority(slow_update_tool) -> None:
    """打断（stop）后 worker 立刻消费 followup inbox 里优先级最高的一条。"""

    async def go() -> tuple[list[str], str]:
        h = Harness()
        h.send("先改库存")
        await asyncio.wait_for(h.tool_started.wait(), TIMEOUT)
        h.send("次急的话", priority=50)
        h.send("加急的话", priority=10)
        h.interrupt()  # 打断当前任务
        await wait_turns(h, 2)  # 被打断的轮 + 立刻接上的新轮
        end = h.turn_ends[0]
        await h.stop()
        return list(h.inputs), str(end.payload.get("reason"))

    order, first_reason = run(go())
    assert first_reason == "interrupted"  # 当前任务被打断
    assert order[1:] == ["加急的话", "次急的话"]  # 立刻按优先级接上


def test_redirect_lands_before_steering(slow_update_tool) -> None:
    """转向（redirect）也是旁路：纠正先落地，steering inbox 的插话跟在后面。"""
    REDIRECT = "先别改库存了，去订会议室"

    async def go() -> tuple[list[str], list[str], int]:
        h = Harness()
        h.send("先改库存")
        await asyncio.wait_for(h.tool_started.wait(), TIMEOUT)
        h.send("常规插话", intent=STEERING, priority=100)
        h.send("加急插话", intent=STEERING, priority=10)
        h.redirect(REDIRECT)
        await asyncio.wait_for(h.steer_seen.wait(), TIMEOUT)
        await h.turn_end.wait()
        steer_texts = list(h.steerings[0].payload["texts"])
        texts = h.history_texts
        await h.stop()
        return steer_texts, texts, len(h.turn_ends)

    steer_texts, texts, turns = run(go())
    assert steer_texts == ["加急插话", "常规插话"]  # 排队插话按优先级
    assert texts.index(REDIRECT) < texts.index("加急插话") < texts.index("常规插话")
    assert turns == 1  # 纠正与插话都进同一个 turn


def test_promote_moves_only_that_message(slow_update_tool) -> None:
    """promote 精确移动一条 followup → steering，别的排队消息不动、顺序不乱。"""

    async def go() -> tuple[bool, list[str], list[list[str]]]:
        h = Harness()
        h.send("先改库存")
        await asyncio.wait_for(h.tool_started.wait(), TIMEOUT)
        # 三条 followup：中间那条被用户点名升级
        h.send("先到", priority=100)
        promoted = UserMessage(
            "user_input", SID, {"text": "要插话"}, intent="followup", priority=50
        )
        h.bus.publish(promoted, to=h.agent.agent_id)
        h.send("后到", priority=100)
        ok = h.agent.promote(promoted)
        await asyncio.wait_for(h.steer_seen.wait(), TIMEOUT)
        await wait_turns(h, 3)  # 本轮（含插话）+ 余下两条 followup
        await h.stop()
        return ok, list(h.inputs), [list(ev.payload["texts"]) for ev in h.steerings]

    ok, order, steerings = run(go())
    assert ok is True
    assert steerings == [["要插话"]]  # 只 drain 被升级的那一条
    assert order == ["先改库存", "先到", "后到"]  # 其余 followup 原序排队


def test_promote_after_consumed_returns_false() -> None:
    """消息已被 worker 取走（正在处理/已处理）：promote 返回 False，无法再插话。"""

    async def go() -> bool:
        h = Harness(script=[FINAL_TEXT])
        sent = UserMessage("user_input", SID, {"text": "只此一条"})
        h.bus.publish(sent, to=h.agent.agent_id)
        await wait_turns(h, 1)  # 已被消费
        ok = h.agent.promote(sent)
        await h.stop()
        return ok

    assert run(go()) is False


def test_steering_class_beats_followup_priority(slow_update_tool) -> None:
    """类序固定：steering inbox 先于 followup inbox 被消费，priority 数字比不过类。

    最后一段流吐字时（本轮不会再有 drain）依次投 M（steering，priority=100）
    和 F2（followup，priority=1）——下一轮的主输入仍是 M：priority 只在同类
    内部排序，不跨类竞争。
    """
    final_with_pause = [
        {"type": "text_delta", "text": "已记录。"},
        {"sleep": 0.4},  # 流中间挂起：两条消息都落在最后一个 drain 之后
        {"type": "text_delta", "text": "收尾。"},
    ]

    async def go() -> tuple[list[str], list[Any]]:
        h = Harness(script=[CALL_INVENTORY, final_with_pause])
        delta_probe = DeltaProbe()
        h.bus.subscribe(
            Subscription("rec-delta", ("agent_delta",), delta_probe)
        )
        h.send("先改库存")
        await asyncio.wait_for(h.tool_started.wait(), TIMEOUT)
        await asyncio.wait_for(delta_probe.first_seen.wait(), TIMEOUT)  # 最后一步在吐字：drain 已过
        h.send("M（steering，priority=100）", intent=STEERING, priority=100)
        h.send("F2（followup，priority=1）", priority=1)
        await wait_turns(h, 3)  # 本轮 + M + F2
        reasons = [e.payload.get("reason") for e in h.turn_ends]
        await h.stop()
        return list(h.inputs), reasons

    order, reasons = run(go())
    assert order == ["先改库存", "M（steering，priority=100）", "F2（followup，priority=1）"]
    assert reasons == ["turn end", "turn end", "turn end"]


class DeltaProbe(StreamConsumer):
    """stream 消费者探针：第一条 delta 到达时置位（MAILBOX_ONLY 要求 StreamConsumer）。"""

    def __init__(self) -> None:
        super().__init__(max_chars=96, frame=0.05)
        self.first_seen = asyncio.Event()

    def offer(self, event: Event) -> None:
        self.first_seen.set()

    def on_flush(self, text: str) -> None:
        pass


def test_promote_missed_boundary_keeps_global_order(slow_update_tool) -> None:
    """promote 错过 turn 的最后一个 drain（最后的流还在吐字时收尾）：不产生顺序反转。

    M 比 F2 先到；M 被 promote 时 turn 的最后一个 drain 已过（脚本在最后一段
    流中间挂起，制造这个窗口）。取件按 (priority, 到达序) 全局合并取最小——
    M 仍先于 F2，而不是 F2 当主输入、M 以插话身份排在后面。
    """
    final_with_pause = [
        {"type": "text_delta", "text": "已记录。"},
        {"sleep": 0.4},  # 流中间挂起：promote 落在最后一个 drain 之后
        {"type": "text_delta", "text": "收尾。"},
    ]

    async def go() -> tuple[bool, list[str]]:
        h = Harness(script=[CALL_INVENTORY, final_with_pause])
        delta_probe = DeltaProbe()
        h.bus.subscribe(
            Subscription("rec-delta", ("agent_delta",), delta_probe)
        )
        h.send("先改库存")
        await asyncio.wait_for(h.tool_started.wait(), TIMEOUT)
        await asyncio.wait_for(delta_probe.first_seen.wait(), TIMEOUT)  # 最后一步在吐字：drain 已过
        promoted = UserMessage(
            "user_input", SID, {"text": "M（先到，promote）"}, intent="followup", priority=100
        )
        h.bus.publish(promoted, to=h.agent.agent_id)
        h.send("F2（后到）", priority=10)
        ok = h.agent.promote(promoted)
        await wait_turns(h, 3)  # 本轮 + M（主输入）+ F2
        await h.stop()
        return ok, list(h.inputs), [list(ev.payload["texts"]) for ev in h.steerings]

    ok, order, steerings = run(go())
    assert ok is True
    assert order == ["先改库存", "M（先到，promote）", "F2（后到）"]  # 到达序保持
    assert steerings == []  # M 被当作主输入消费，没有作为插话 drain
