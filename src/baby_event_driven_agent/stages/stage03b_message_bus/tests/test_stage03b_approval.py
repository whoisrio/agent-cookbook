"""Stage 4 人工确认用例：全部离线。

要验的是"等答复"这一半：批准、拒绝、批准并改写、超时、等待中被中断、迟到的答复。
策略（要不要问人）是当场判的，答复是从外面给的——所以每条用例都是
"脚本化 LLM 触发一次工具调用 + 一个假的'人'看到 approval_required 再答复"。
真的打模型没有意义：这里要的是确定性，不是 provider 收不收这套消息形状。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from baby_event_driven_agent.stages.stage03b_message_bus import agent as agent_mod
from baby_event_driven_agent.stages.stage03b_message_bus.agent import Agent
from baby_event_driven_agent.stages.stage03b_message_bus.bus import EventBus
from baby_event_driven_agent.stages.stage03b_message_bus.events import (
    Event,
    Subscription,
    UserMessage,
)
from baby_event_driven_agent.stages.stage03b_message_bus.tools import TOOLS

TIMEOUT = 15.0
SID = "A"

CALL_INVENTORY = [
    {
        "type": "tool_call_delta",
        "index": 0,
        "id": "call_1",
        "name": "update_inventory",
        "args_delta": '{"category": "保温杯", "stock": 45}',
    }
]
FINAL_TEXT = [{"type": "text_delta", "text": "已处理。"}]


class ScriptedLLM:
    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls = 0

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for chunk in self.script[idx]:
            yield chunk


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage03b_approval_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def inventory_spy() -> list[dict[str, Any]]:
    """写工具换成只记录、不落盘的探针：断言"到底执行没有"就够了，
    不让用例去改仓库里的知识库文件。"""
    calls: list[dict[str, Any]] = []
    original = TOOLS["update_inventory"]

    async def spy(args: dict[str, Any]) -> str:
        calls.append(args)
        return f"已更新：{args.get('category')}：库存 {args.get('stock')} 件"

    TOOLS["update_inventory"] = replace(original, fn=spy)
    yield calls
    TOOLS["update_inventory"] = original


class Case:
    """一个装了"人"的台子：谁问、谁答、什么时候结束，都在这里。

    要不要审批不用台子操心——update_inventory 在 tools.py 里自己声明了
    `requires_approval=True`，agent 执行前查声明。
    """

    def __init__(self, workdir: Path, *, timeout: float = 5.0) -> None:
        self.bus = EventBus()
        self.agent = Agent(
            self.bus,
            ScriptedLLM([CALL_INVENTORY, FINAL_TEXT]),
            approval_timeout=timeout,
        )
        self.asked: asyncio.Queue[Event] = asyncio.Queue()
        self.ended = asyncio.Event()
        self.end_event: Event | None = None
        self.decided_events: list[Event] = []
        self.bus.subscribe(
            Subscription("rec-ask", ("approval_required",), self._record_ask)
        )
        self.bus.subscribe(
            Subscription("rec-decided", ("approval_decided",), self._record_decided)
        )
        self.bus.subscribe(
            Subscription("rec-end", ("turn_end",), self._record_end)
        )

    async def _record_ask(self, event: Event) -> None:
        self.asked.put_nowait(event)

    async def _record_decided(self, event: Event) -> None:
        self.decided_events.append(event)

    async def _record_end(self, event: Event) -> None:
        self.end_event = event
        self.ended.set()

    def send(self) -> None:
        self.bus.publish(
            UserMessage("user_input", SID, {"text": "把保温杯库存改成 45 件"}), to=self.agent.agent_id
        )

    async def wait_ask(self) -> Event:
        return await asyncio.wait_for(self.asked.get(), TIMEOUT)

    def answer(self, request_id: str, approve: bool, **extra: Any) -> None:
        self.bus.publish(
            Event(
                "user_approval",
                SID,
                {"request_id": request_id, "approve": approve, **extra},
            ),
            to=self.agent.agent_id,
        )

    def stop(self, intent: str = "stop") -> None:
        self.bus.publish(Event("user_interrupt", SID, {"intent": intent}), to=self.agent.agent_id)

    async def settle(self) -> None:
        """等人答完、turn 收尾。"""
        await asyncio.wait_for(self.ended.wait(), TIMEOUT)
        await self.agent.stop()

    def tool_messages(self) -> list[str]:
        return [
            str(m.get("content", ""))
            for m in self.agent.history[SID]
            if m.get("role") == "tool"
        ]

    def decided(self) -> list[dict[str, Any]]:
        return [ev.payload for ev in self.decided_events]


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(asyncio.wait_for(coro, TIMEOUT))


async def drive(case: Case, answer):  # type: ignore[no-untyped-def]
    case.send()
    asked = await case.wait_ask()
    answer(case, asked)


# ------------------------------------------------------------------ 三种答复


def test_approved_executes_the_tool(workdir: Path, inventory_spy: list[dict[str, Any]]) -> None:
    """批准 → 工具真的执行，确认的请求与回执都是事件、靠 request_id 配对。"""
    case = Case(workdir)

    async def go() -> None:
        await drive(case, lambda c, ask: c.answer(ask.payload["request_id"], True))
        await case.settle()

    run(go())
    assert inventory_spy == [{"category": "保温杯", "stock": 45}]
    assert case.tool_messages() and case.tool_messages()[0].startswith("已更新")
    assert case.end_event is not None and case.end_event.payload["reason"] == "turn end"
    assert len(case.decided_events) == 1
    decided = case.decided()[0]
    assert (decided["action"], decided["by"]) == ("allow", "user")


def test_rejected_blocks_the_tool(workdir: Path, inventory_spy: list[dict[str, Any]]) -> None:
    """拒绝 → 工具不执行，模型拿到一条自描述占位。"""
    case = Case(workdir)

    async def go() -> None:
        await drive(
            case,
            lambda c, ask: c.answer(ask.payload["request_id"], False, reason="改库存要走审批单"),
        )
        await case.settle()

    run(go())
    assert inventory_spy == []
    assert case.tool_messages()[0].startswith(agent_mod.APPROVAL_REJECTED)
    assert "改库存要走审批单" in case.tool_messages()[0]
    assert (case.decided()[0]["action"], case.decided()[0]["by"]) == ("deny", "user")


def test_approved_with_rewritten_arguments(workdir: Path, inventory_spy: list[dict[str, Any]]) -> None:
    """人的裁决不只有"批/拒"：批了但把参数改掉，工具按改过的执行。"""
    case = Case(workdir)

    async def go() -> None:
        await drive(
            case,
            lambda c, ask: c.answer(
                ask.payload["request_id"],
                True,
                arguments='{"category": "玻璃杯", "stock": 7}',
            ),
        )
        await case.settle()

    run(go())
    assert inventory_spy == [{"category": "玻璃杯", "stock": 7}]
    assert case.decided()[0]["action"] == "modify"


# ------------------------------------------------------------------ 等不到人的时候


def test_timeout_is_fail_closed(workdir: Path, inventory_spy: list[dict[str, Any]]) -> None:
    """等不到答复：按拒绝处理（fail-closed），不是默认放行。"""
    case = Case(workdir, timeout=0.15)

    async def go() -> None:
        case.send()  # 没人理
        await case.settle()

    run(go())
    assert inventory_spy == []
    assert case.tool_messages()[0].startswith(agent_mod.APPROVAL_TIMEOUT)
    decided = case.decided()[0]
    assert (decided["action"], decided["by"]) == ("deny", "approval_timeout")
    assert case.end_event is not None and case.end_event.payload["reason"] == "turn end"


def test_interrupt_during_the_wait(workdir: Path, inventory_spy: list[dict[str, Any]]) -> None:
    """等确认的时候用户按停止：不执行、turn 按 interrupted 收尾，且**回执照样有**。"""
    case = Case(workdir, timeout=30.0)

    async def go() -> None:
        case.send()
        await case.wait_ask()
        case.stop()  # 人不答了，改主意
        await case.settle()

    run(go())
    assert inventory_spy == []
    assert case.tool_messages() == [agent_mod.APPROVAL_ABANDONED]
    assert case.agent.history[SID][-1]["content"] == agent_mod.STOP_CLOSER
    assert case.end_event is not None and case.end_event.payload["reason"] == "interrupted"
    # 一问必有一答：被中断放弃也是一种结局，log 里不能只有请求没有回执
    decided = case.decided()
    assert len(decided) == 1
    assert (decided[0]["action"], decided[0]["by"]) == ("abandoned", "user_interrupt")


def test_stale_reply_is_not_claimed(workdir: Path, inventory_spy: list[dict[str, Any]]) -> None:
    """号不对 / 迟到的答复：不认领、不伪造裁决，等的那条继续走到超时。

    迟到输入怎么留痕（approval_reply 进账）是下一章事件账的事，本章只保证
    它不会误伤任何一次等待。
    """
    case = Case(workdir, timeout=0.2)
    asked_ids: list[str] = []

    async def go() -> None:
        case.send()
        asked = await case.wait_ask()
        asked_ids.append(str(asked.payload["request_id"]))
        case.answer("ap-00000000", True)  # 别人的号：没有人在等它
        await asyncio.sleep(0.35)  # 先让它超时（这段等待是真的走完了）
        case.answer(asked_ids[0], True)  # 迟到的答复
        await case.settle()

    run(go())
    assert inventory_spy == []  # 迟到的"同意"救不回已经按拒绝走完的那次调用
    assert case.tool_messages()[0].startswith(agent_mod.APPROVAL_TIMEOUT)
    # 不认领 ≠ 伪造裁决：那次确认的回执只有超时那一条
    assert [(d["action"], d["by"]) for d in case.decided()] == [("deny", "approval_timeout")]
