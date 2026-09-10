"""Stage 1 测试：总线分发、流式 agent 主循环、写工具真改文件、append-only log。

直连本地模型（ollama qwen3.5:4b-32k，OpenAI 兼容端点）：demo 和 tests
用同一个 RealLLM，不养替身。断言只锁行为轮廓——turn 完成、流式增量
到达、log 结构、文件真的被写——不锁模型的具体措辞。
模型不可达（ollama 没起）时跳过并说明，不算失败。
不用 pytest 的 tmp_path fixture（WorkBuddy 沙箱 shim 会拦
pytest-of-unknown 的 mkdir），用 tempfile.mkdtemp 自建临时目录。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from baby_event_driven_agent.stages.stage01_receive_events.agent import Agent
from baby_event_driven_agent.stages.stage01_receive_events.events import (
    Event,
    EventBus,
    SessionLog,
)
from baby_event_driven_agent.stages.stage01_receive_events.llm import RealLLM

# 测试固定打本地模型（环境变量优先于 .env，这正是 llm.py 约定的覆盖机制）
_LOCAL = {
    "OPENAI_API_BASE": "http://localhost:11434/v1",
    "OPENAI_API_KEY": "ollama",
    "OPENAI_MODEL": "qwen3.5:4b-32k",
}


async def _model_reachable() -> bool:
    """发一个 1 token 的请求探活：本地模型没起就跳过测试。"""
    import openai

    try:
        client = openai.AsyncOpenAI(
            api_key="ollama", base_url=_LOCAL["OPENAI_API_BASE"], timeout=10.0
        )
        await client.chat.completions.create(
            model=_LOCAL["OPENAI_MODEL"],
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def reachable() -> bool:
    return asyncio.run(_model_reachable())


@pytest.fixture(autouse=True)
def _local_env(reachable: bool) -> None:
    """探活之后再把测试端点写进环境变量，RealLLM 建实例时读到。"""
    os.environ.update(_LOCAL)


@pytest.fixture()
def log_path() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage01_test_"))
    yield d / "session.jsonl"
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def inventory_backup() -> AsyncIterator[Path]:
    """写测试会真改 inventory.txt：跑前备份，跑后还原。"""
    inv = (
        Path(__file__).resolve().parents[3] / "knowledge-base" / "inventory.txt"
    )  # src/baby_event_driven_agent/knowledge-base/，三个 stage 共享
    backup = Path(tempfile.mkdtemp(prefix="stage01_inv_")) / "inventory.txt"
    shutil.copy(inv, backup)
    yield inv
    shutil.copy(backup, inv)
    shutil.rmtree(backup.parent, ignore_errors=True)


def _make_bus_with_agent(log_path: Path) -> EventBus:
    bus = EventBus()
    agent = Agent(bus, SessionLog(str(log_path)), RealLLM())
    bus.subscribe("user_input", agent.on_user_input)
    return bus


def _ask(bus: EventBus, text: str) -> tuple[list[str], list[dict]]:
    """发一轮 user_input，顺带收集 delta 和 reply。"""
    deltas: list[str] = []
    replies: list[dict] = []

    async def collect_delta(e: Event) -> None:
        deltas.append(e.payload["text"])

    async def collect_reply(e: Event) -> None:
        replies.append(e.payload["message"])

    bus.subscribe("agent_delta", collect_delta)
    bus.subscribe("agent_reply", collect_reply)

    async def run() -> None:
        await bus.publish(Event("user_input", "A", {"text": text}))

    asyncio.run(run())
    return deltas, replies


def test_query_turn_streams_and_logs(log_path: Path, reachable: bool) -> None:
    """查询一轮：流式增量到达，模型选了工具，log 有头有尾。"""
    if not reachable:
        pytest.skip("本地模型不可达（ollama 没起）：stage01 测试直连真实模型")

    bus = _make_bus_with_agent(log_path)
    deltas, replies = _ask(bus, "保温杯还有库存吗")

    assert len(deltas) > 0, "流式增量一块都没到"
    assert any(r.get("tool_calls") for r in replies), "模型没有发起任何工具调用"

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert records[0]["type"] == "user_input"
    assert records[-1]["type"] == "turn_end"
    assert all(r["session"] == "A" for r in records)
    # 中间记录只有两种：agent_reply（要么带工具调用、要么是最终回答）、
    # tool_result（工具真的执行过，返回进了轨迹）
    for r in records[1:-1]:
        assert r["type"] in ("agent_reply", "tool_result"), r
    assert any(r["type"] == "tool_result" for r in records), "工具返回没进 log"


def test_write_tool_really_changes_file(
    log_path: Path, inventory_backup: Path, reachable: bool
) -> None:
    """写一轮：update_inventory 真写文件，下一轮查询读到新数据。"""
    if not reachable:
        pytest.skip("本地模型不可达（ollama 没起）：stage01 测试直连真实模型")

    bus = _make_bus_with_agent(log_path)
    _, replies1 = _ask(bus, "帮我把马克杯加进库存：8 件，陶瓷，350ml")
    assert any(
        any(c["function"]["name"] == "update_inventory" for c in r["tool_calls"])
        for r in replies1
        if r.get("tool_calls")
    ), "模型没有调用 update_inventory"
    # 文件真的变了
    assert "马克杯" in inventory_backup.read_text(encoding="utf-8")

    _, replies2 = _ask(bus, "马克杯还有货吗")
    finals = [r for r in replies2 if r.get("content") and not r.get("tool_calls")]
    assert finals and "8" in finals[-1]["content"], "第二轮没有读到刚写入的库存"


def test_event_log_is_append_only(log_path: Path, reachable: bool) -> None:
    """log 只追加：写完之后前面的行一字不改。"""
    if not reachable:
        pytest.skip("本地模型不可达（ollama 没起）：stage01 测试直连真实模型")

    bus = _make_bus_with_agent(log_path)
    _ask(bus, "会议室怎么订")
    first = log_path.read_text()

    _ask(bus, "VPN 怎么申请")
    second = log_path.read_text()

    assert second.startswith(first)
