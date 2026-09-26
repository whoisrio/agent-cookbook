"""手动压缩测试：刀口、拒绝第二刀、端到端视图切换、resume × 压缩 × 悬挂修复。

压缩的投影语义（刀口前跳过、摘要插最前、认第一刀）在 5a 已测
（test_stage04_trajectory.py）；这里测的是机制层：

- cut_before_turn：刀口落在倒数第 N 轮的第一条真实 user entry；合成 user 不算开轮；
  轮数不足返回 None
- maybe_compact：已有压缩拒绝第二刀（折叠归 05）；摘要输入 = 刀口前视图
  （含 branch_summary 的 <summary>——"摘要吞摘要"）
- LiveSummarizer：裸 chat（tools=None）+ max_tokens 封顶，渲染含工具调用发起
- agent 端到端：compact_request → step 边界压缩 → 下一次调用就是新视图
- resume × 压缩：恢复出来的是压缩视图不是全量原文；悬挂调用补占位；
  原文件 append-only（resume 前字节是 resume 后的前缀）
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from baby_event_driven_agent.stages.stage04_trajectory import tools as tools_mod
from baby_event_driven_agent.stages.stage04_trajectory.agent import (
    UNKNOWN_TOOL_RESULT,
    Agent,
    build_context,
)
from baby_event_driven_agent.stages.stage04_trajectory.main import (
    LONG_TASK_SCRIPT,
    LONG_TASK_TURNS,
)
from baby_event_driven_agent.stages.stage04_trajectory.session.compaction import (
    LiveSummarizer,
    ScriptedSummarizer,
    cut_before_turn,
    maybe_compact,
)
from baby_event_driven_agent.stages.stage04_trajectory.session.store import SessionStore
from baby_event_driven_agent.stages.stage04_trajectory.session.trajectory import (
    COMPACTION,
    MESSAGE,
    Trajectory,
    TrajectoryLog,
    message_payload,
)
from baby_event_driven_agent.stages.stage04_trajectory.transport.bus import EventBus
from baby_event_driven_agent.stages.stage04_trajectory.transport.events import (
    OBSERVE,
    Event,
    Subscription,
)
from baby_event_driven_agent.stages.stage04_trajectory.transport.persistence import EventLog

TIMEOUT = 10.0


def _new_traj(workdir: Path, name: str) -> Trajectory:
    return Trajectory.create(TrajectoryLog(workdir / name), sid="s1")


def _user_entry(traj: Trajectory, text: str):
    return traj.append(MESSAGE, message_payload({"role": "user", "content": text}))


def _hand_traj(workdir: Path, name: str) -> Trajectory:
    """三轮手搓会话：user / assistant × 3。"""
    traj = _new_traj(workdir, name)
    _user_entry(traj, "第一轮")
    traj.append(MESSAGE, message_payload({"role": "assistant", "content": "答一"}))
    _user_entry(traj, "第二轮")
    traj.append(MESSAGE, message_payload({"role": "assistant", "content": "答二"}))
    _user_entry(traj, "第三轮")
    traj.append(MESSAGE, message_payload({"role": "assistant", "content": "答三"}))
    return traj


# ------------------------------------------------------------------ 刀口


def test_cut_before_turn_lands_on_turn_start(workdir: Path) -> None:
    traj = _hand_traj(workdir, "a.jsonl")
    path = traj.path()
    u2 = [e for e in path if e.type == MESSAGE and e.payload["message"]["content"] == "第二轮"][0]
    u3 = [e for e in path if e.type == MESSAGE and e.payload["message"]["content"] == "第三轮"][0]
    assert cut_before_turn(path, keep_turns=2) == u2.id  # 保留二三两轮
    assert cut_before_turn(path, keep_turns=1) == u3.id
    assert cut_before_turn(path, keep_turns=5) is None  # 轮数不足：无可压段


def test_cut_before_turn_ignores_synthetic_user(workdir: Path) -> None:
    traj = _new_traj(workdir, "b.jsonl")
    _user_entry(traj, "一")
    # redirect 的合成 user（打断转向时补的）不算开轮
    traj.append(
        MESSAGE,
        message_payload({"role": "user", "content": "插话"}, synthetic=True, note="redirect"),
    )
    _user_entry(traj, "二")
    path = traj.path()
    assert cut_before_turn(path, keep_turns=2) is None  # 真实轮只有 2 个
    u2 = [e for e in path if e.type == MESSAGE and e.payload["message"]["content"] == "二"][0]
    assert cut_before_turn(path, keep_turns=1) == u2.id


# ------------------------------------------------------------------ maybe_compact


def test_maybe_compact_refuses_second_cut(workdir: Path) -> None:
    traj = _hand_traj(workdir, "c.jsonl")
    summarizer = ScriptedSummarizer(["摘要一"])
    entry = asyncio.run(
        maybe_compact(traj, summarizer, keep_turns=1, prefix_view=Agent._prefix_view)
    )
    assert entry is not None
    second = asyncio.run(
        maybe_compact(traj, summarizer, keep_turns=1, prefix_view=Agent._prefix_view)
    )
    assert second is None  # 拒绝第二刀：折叠语义归 05
    assert [e.type for e in traj.entries()].count(COMPACTION) == 1


def test_maybe_compact_nothing_to_compress(workdir: Path) -> None:
    traj = _new_traj(workdir, "d.jsonl")
    _user_entry(traj, "唯一一轮")
    entry = asyncio.run(
        maybe_compact(traj, ScriptedSummarizer(["x"]), keep_turns=2, prefix_view=Agent._prefix_view)
    )
    assert entry is None


def test_maybe_compact_segment_includes_branch_summary(workdir: Path) -> None:
    """摘要输入 = 刀口前视图：branch_summary 的 <summary> 也在——摘要吞摘要。"""
    traj = _hand_traj(workdir, "e.jsonl")
    u1 = traj.path()[1]  # 第一条 user（0 是 session_started）
    traj.branch_with_summary(u1.id, "试过 X，结论 Y")
    _user_entry(traj, "重来的第一轮")
    traj.append(MESSAGE, message_payload({"role": "assistant", "content": "答"}))
    _user_entry(traj, "重来的第二轮")
    summarizer = ScriptedSummarizer(["摘要"])
    entry = asyncio.run(
        maybe_compact(traj, summarizer, keep_turns=1, prefix_view=Agent._prefix_view)
    )
    assert entry is not None
    seg_texts = [str(m.get("content")) for m in summarizer.segments[0]]
    assert any("<summary>试过 X，结论 Y</summary>" in t for t in seg_texts)  # 遗言进摘要输入
    assert not any("重来的第二轮" in t for t in seg_texts)  # 保留窗不进摘要输入
    assert entry.payload["keep_from_id"] == traj.path()[-2].id  # 刀口 = 倒数第一轮 user


# ------------------------------------------------------------------ LiveSummarizer


class _RecordingLLM:
    """记录 stream_chat 收到的 kwargs；吐两段文本增量。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def stream_chat(self, messages: list[dict[str, Any]], **kwargs: Any):  # type: ignore[no-untyped-def]
        self.calls.append({"messages": messages, **kwargs})
        for text in ("【已完成】", "查过库存"):
            yield {"type": "text_delta", "text": text}


def test_live_summarizer_bare_chat_and_cap() -> None:
    llm = _RecordingLLM()
    summarizer = LiveSummarizer(llm, max_tokens=256)
    out = asyncio.run(
        summarizer.summarize(
            [
                {"role": "user", "content": "查一下保温杯"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "query_inventory",
                                "arguments": '{"category": "保温杯"}',
                            },
                        }
                    ],
                },
            ]
        )
    )
    assert out == "【已完成】查过库存"
    call = llm.calls[0]
    assert call["tools"] is None  # 裸 chat：不带工具
    assert call["max_tokens"] == 256  # 封顶
    rendered = call["messages"][-1]["content"]
    assert "query_inventory" in rendered  # 工具调用发起可见
    assert "[user] 查一下保温杯" in rendered


# ------------------------------------------------------------------ agent 端到端


class ScriptedLLM:
    """最小脚本化 LLM（与 agent 用例同款）。"""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.calls = 0
        self.contexts: list[list[dict[str, Any]]] = []

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        self.contexts.append([dict(m) for m in messages])
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for chunk in self.script[idx]:
            yield chunk


def _tool_step(cid: str, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": cid,
            "name": name,
            "args_delta": json.dumps(args, ensure_ascii=False),
        }
    ]


def _text_step(text: str) -> list[dict[str, Any]]:
    return [{"type": "text_delta", "text": text}]


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage04_compact_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def data_copies(workdir: Path) -> None:
    """数据源换到工作目录副本；结束还原（写操作不碰包自带 data/）。"""
    attrs = ("_INVENTORY", "_RULES", "_TASKS")
    saved = {a: getattr(tools_mod, a) for a in attrs}
    for a in attrs:
        src: Path = saved[a]
        dst = workdir / src.name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        setattr(tools_mod, a, dst)
    yield
    for a in attrs:
        setattr(tools_mod, a, saved[a])


def test_manual_compaction_end_to_end(workdir: Path, data_copies: None) -> None:
    """长任务五轮跑完后 compact_request：边界压缩，下一次调用就是新视图。

    断言：compaction entry（摘要 = 剧本、刀口 = 倒数第二轮 user）；
    context_compacted 事件（消息数下降）；新视图 = [system, <摘要>, 保留窗…]；
    摘要输入 = 刀口前视图；审批账目不受影响。
    """
    saved = {a: getattr(tools_mod, a) for a in ("_INVENTORY", "_RULES", "_TASKS")}
    try:
        for a, p in saved.items():
            copy = workdir / p.name
            copy.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            setattr(tools_mod, a, copy)
        log = EventLog(str(workdir / "events"))
        bus = EventBus(log)
        store = SessionStore(workdir / "sessions")
        summarizer = ScriptedSummarizer(
            [
                "【已完成】T-101 补货核查处理完：保温杯此前已直接补到 50（副作用，勿重做）；"
                "玻璃杯补到 20；马克杯报备被拒未补；保温壶补到 20。"
                "【待办】T-102 盘点差异单处理中。"
            ]
        )
        traj = store.start(cwd=str(workdir), system_prompt="SYS")
        agent = Agent(
            bus,
            ScriptedLLM(LONG_TASK_SCRIPT),
            store=store,
            summarizer=summarizer,
            system_prompt="SYS",
        )
        agent.attach(traj)
        events: list[Event] = []
        ended = asyncio.Event()

        async def on_end(event: Event) -> None:
            ended.set()

        async def on_any(event: Event) -> None:
            events.append(event)

        async def on_required(event: Event) -> None:
            bus.publish(
                Event(
                    "user_approval",
                    traj.sid,
                    {
                        "request_id": event.payload["request_id"],
                        "approve": False,
                        "reason": "数量过大，本次不补",
                    },
                ),
                to=agent.agent_id,
            )

        bus.subscribe(Subscription("end", ("turn_end",), on_end, mode=OBSERVE))
        bus.subscribe(Subscription("any", ("*",), on_any, mode=OBSERVE))
        bus.subscribe(Subscription("req", ("approval_required",), on_required))

        async def go() -> None:
            for text in LONG_TASK_TURNS:
                bus.publish(Event("user_input", traj.sid, {"text": text}), to=agent.agent_id)
                await asyncio.wait_for(ended.wait(), TIMEOUT)
                ended.clear()
            # 幕 6：压一下
            bus.publish(
                Event("compact_request", traj.sid, {"reason": "manual"}), to=agent.agent_id
            )
            bus.publish(Event("user_input", traj.sid, {"text": "继续"}), to=agent.agent_id)
            await asyncio.wait_for(ended.wait(), TIMEOUT)
            ended.clear()

        asyncio.run(asyncio.wait_for(go(), TIMEOUT))

        # compaction entry：摘要 = 剧本，刀口 = 倒数第二轮 user
        comps = [e for e in traj.entries() if e.type == COMPACTION]
        assert len(comps) == 1
        assert comps[0].payload["summary"].startswith("【已完成】")
        assert comps[0].payload["reason"] == "manual"
        real_users = [
            e
            for e in traj.path()
            if e.type == MESSAGE
            and e.payload["message"]["role"] == "user"
            and not e.payload.get("synthetic")
        ]
        assert comps[0].payload["keep_from_id"] == real_users[-2].id
        # 事件：恰好一次成功，消息数下降
        compacted = [e for e in events if e.type == "context_compacted"]
        failed = [e for e in events if e.type == "context_compact_failed"]
        assert len(compacted) == 1 and not failed
        assert compacted[0].payload["messages_after"] < compacted[0].payload["messages_before"]
        # 下一次调用的上下文 = [system, <摘要>, 保留窗…]；且比压缩前最后一次调用短
        assert agent.llm.contexts[-1][1]["content"].startswith("<summary>")
        assert len(agent.llm.contexts[-1]) < len(agent.llm.contexts[-2])
        # 摘要输入 = 刀口前视图：含第一轮、不含保留窗的最后一轮
        seg_texts = [str(m.get("content")) for m in summarizer.segments[0]]
        assert any("今天仓库的补货核查" in t for t in seg_texts)
        assert not any("把剩下的处理完" in t for t in seg_texts)
        # 审批账目不受影响：仍然恰好一次拒绝
        decided = [e for e in events if e.type == "approval_decided"]
        assert [e.payload.get("action") for e in decided] == ["deny"]
    finally:
        for a, p in saved.items():
            setattr(tools_mod, a, p)


def test_resume_after_compaction_restores_compacted_view(workdir: Path) -> None:
    """resume × 压缩：恢复出来的是压缩视图（不是全量原文），悬挂调用补占位，
    原文件 append-only（resume 前字节是 resume 后的前缀）。"""
    store = SessionStore(workdir / "sessions")
    traj = store.start(cwd=str(workdir), system_prompt="SYS")
    summarizer = ScriptedSummarizer(["【已完成】早期的事都办完了。【待办】围巾待核对。"])
    _user_entry(traj, "第一轮")
    traj.append(MESSAGE, message_payload({"role": "assistant", "content": "答一"}))
    _user_entry(traj, "第二轮")
    traj.append(MESSAGE, message_payload({"role": "assistant", "content": "答二"}))
    _user_entry(traj, "第三轮：核对围巾")
    entry = asyncio.run(
        maybe_compact(traj, summarizer, keep_turns=1, reason="manual", prefix_view=Agent._prefix_view)
    )
    assert entry is not None
    # 崩在工具执行前：assistant 要了围巾的数据，结果永远没来
    traj.append(
        MESSAGE,
        message_payload(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_d1",
                        "type": "function",
                        "function": {"name": "query_inventory", "arguments": '{"category": "围巾"}'},
                    }
                ],
            }
        ),
    )
    bytes_before = traj.log.raw_bytes()

    resumed = store.resume(traj.sid, note="crash 恢复演练")
    proj = build_context(resumed)
    # 压缩视图：system + <摘要> + 保留窗（不是全量原文——第一轮不在）
    assert proj.messages[1]["content"].startswith("<summary>")
    assert not any("第一轮" in str(m.get("content")) for m in proj.messages)
    # 悬挂调用补占位（自描述），原 entry 原封不动
    assert any(
        m.get("role") == "tool" and m.get("content") == UNKNOWN_TOOL_RESULT for m in proj.messages
    )
    # append-only：resume 前字节是 resume 后的前缀（只追加了 session_resumed）
    assert resumed.log.raw_bytes().startswith(bytes_before)
