"""Stage 1 测试：总线分发、流式 agent 主循环、append-only session log。

离线跑：用与 RealLLM 同协议的 FakeLLM，arguments 拆两块发，
把增量累积逻辑也测到。不用 pytest 的 tmp_path fixture（WorkBuddy
沙箱 shim 会拦 pytest-of-unknown 的 mkdir），用 tempfile.mkdtemp
自建临时目录，测试结束自己清理。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage01_receive_events.agent import Agent
from baby_event_driven_agent.stages.stage01_receive_events.events import (
    Event,
    EventBus,
    SessionLog,
)
from baby_event_driven_agent.stages.stage01_receive_events.llm import FakeLLM


@pytest.fixture()
def log_path() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage01_test_"))
    yield d / "session.jsonl"
    shutil.rmtree(d, ignore_errors=True)


def _make_bus_with_agent(log_path: Path) -> EventBus:
    bus = EventBus()
    agent = Agent(bus, SessionLog(str(log_path)), FakeLLM())
    bus.subscribe("user_input", agent.on_user_input)
    return bus


def test_sequential_turns_stream_and_log(log_path: Path) -> None:
    """顺序两轮问答：文本分块流式到达，最终回答完整，log 记录 turn start/end。"""
    bus = _make_bus_with_agent(log_path)
    deltas: list[str] = []
    replies: list[dict] = []

    async def collect_delta(e: Event) -> None:
        deltas.append(e.payload["text"])

    async def collect_reply(e: Event) -> None:
        replies.append(e.payload["message"])

    bus.subscribe("agent_delta", collect_delta)
    bus.subscribe("agent_reply", collect_reply)

    async def run() -> None:
        await bus.publish(Event("user_input", "A", {"text": "保温杯还有库存吗"}))
        await bus.publish(Event("user_input", "A", {"text": "玻璃杯呢"}))

    asyncio.run(run())

    # 流式：agent_delta 多块到达，拼起来是完整的最终回答
    assert len(deltas) > 2
    assert "".join(deltas).startswith("根据检索结果回答：")

    # 每轮两条 agent_reply：一条 tool_call、一条最终回答
    tool_calls = [r for r in replies if r.get("tool_calls")]
    finals = [r for r in replies if r.get("content") and not r.get("tool_calls")]
    assert len(tool_calls) == 2
    assert len(finals) == 2
    # tool_call 的 arguments 是分块累积出来的合法 JSON
    for tc in tool_calls:
        call = tc["tool_calls"][0]
        assert json.loads(call["function"]["arguments"]) == {"query": "杯子"}

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [r["type"] for r in records] == [
        "user_input",
        "agent_reply",  # tool_call
        "agent_reply",  # 最终回答
        "turn_end",
        "user_input",
        "agent_reply",
        "agent_reply",
        "turn_end",
    ]
    assert [r.get("note") for r in records if r["type"] == "agent_reply"] == [
        "tool_call",
        "final",
        "tool_call",
        "final",
    ]
    assert all(r["session"] == "A" for r in records)


def test_sessions_have_independent_histories(log_path: Path) -> None:
    """不同 session 的 history 互不污染。"""
    bus = _make_bus_with_agent(log_path)

    async def run() -> None:
        await bus.publish(Event("user_input", "A", {"text": "保温杯还有库存吗"}))
        await bus.publish(Event("user_input", "B", {"text": "帮我下单"}))

    asyncio.run(run())

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    sessions = {r["session"] for r in records}
    assert sessions == {"A", "B"}
    # 每个 session 各自完整一轮（B 问"帮我下单"，不触发工具，一轮一答）
    a_types = [r["type"] for r in records if r["session"] == "A"]
    b_types = [r["type"] for r in records if r["session"] == "B"]
    assert a_types == ["user_input", "agent_reply", "agent_reply", "turn_end"]
    assert b_types == ["user_input", "agent_reply", "turn_end"]


def test_event_log_is_append_only(log_path: Path) -> None:
    """log 只追加：写完之后前面的行一字不改。"""
    bus = _make_bus_with_agent(log_path)

    async def run() -> None:
        await bus.publish(Event("user_input", "A", {"text": "保温杯还有库存吗"}))

    asyncio.run(run())
    first = log_path.read_text()

    asyncio.run(bus.publish(Event("user_input", "A", {"text": "玻璃杯呢"})))
    second = log_path.read_text()

    assert second.startswith(first)
