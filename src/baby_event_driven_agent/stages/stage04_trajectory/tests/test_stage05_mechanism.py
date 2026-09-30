"""05 增量的机制单测（离线，不依赖真模型）。

实现与 04 共用一份（session/compaction.py / agent.py / llm.py），本文件把 05 章
「代码改动」清单的机制点逐个钉死：

- 计量：estimate_tokens 字符估算（不引 tokenizer）+ TokenMeter 锚点 + 轨迹增量
- 水位：ratio / reserve 两种触发线；window=0 不触发
- 刀口：cut_before_step 落在倒数第 N 个 step 起点、tool 配对完整、
  keep_tokens 预算任一用尽即停、纯聊天退回 turn 刀口
- 折叠：第二刀吞掉旧摘要（previous 传递 + 原文不重读）、投影认最后一切、
  rewind 过刀口语义不变
- 复检：摘要+保留窗超触发线时收缩保留窗，收到 1 个 step 仍超线不硬压
- agent：水位自动压缩（step 边界）、max_steps 可配置（默认 20）
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from baby_event_driven_agent.stages.stage04_trajectory import tools as tools_mod
from baby_event_driven_agent.stages.stage04_trajectory.agent import Agent, build_context
from baby_event_driven_agent.stages.stage04_trajectory.session.compaction import (
    COMPRESSION_SYSTEM,
    CompactionPolicy,
    ScriptedSummarizer,
    TokenMeter,
    cut_before_step,
    cut_before_turn,
    estimate_tokens,
    maybe_compact,
    serialize_segment,
    trigger_tokens,
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


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage05_mech_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def data_copies(workdir: Path) -> None:
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


# ---------------------------------------------------------------- 轨迹脚手架


def _user(traj: Trajectory, text: str):
    return traj.append(MESSAGE, message_payload({"role": "user", "content": text}))


def _answer(traj: Trajectory, text: str):
    return traj.append(MESSAGE, message_payload({"role": "assistant", "content": text}))


def _call(traj: Trajectory, cid: str, name: str, args: str):
    return traj.append(
        MESSAGE,
        message_payload(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}
                ],
            }
        ),
    )


def _result(traj: Trajectory, cid: str, text: str):
    return traj.append(
        MESSAGE, message_payload({"role": "tool", "tool_call_id": cid, "content": text})
    )


def _step_traj(workdir: Path, name: str, n: int = 3) -> Trajectory:
    """n 个 step 的会话：每轮 user → assistant(tool_call) → tool → assistant(答复)。"""
    traj = Trajectory.create(TrajectoryLog(workdir / name), sid="s1")
    calls = []
    for i in range(1, n + 1):
        _user(traj, f"第{i}轮的问题")
        calls.append(_call(traj, f"c{i}", "query_inventory", f'{{"category": "品类{i}"}}'))
        _result(traj, f"c{i}", f"品类{i} 的查询结果")
        _answer(traj, f"第{i}轮的答复")
    return traj


def _step_starts(traj: Trajectory) -> list[str]:
    """路径上的 step 起点：带 tool_calls 的 assistant entry，按路径顺序。"""
    return [
        e.id
        for e in traj.path()
        if e.type == MESSAGE and e.payload["message"].get("tool_calls")
    ]


# ---------------------------------------------------------------- 计量


def test_estimate_tokens_no_tokenizer() -> None:
    """CJK 按字计、ASCII 按 4 字符计、每条消息固定开销；tool_calls 计入。"""
    cjk = estimate_tokens([{"role": "user", "content": "四个汉字"}])
    ascii_ = estimate_tokens([{"role": "user", "content": "abcdefgh"}])  # 8 / 4 = 2
    assert cjk == 4 + 4  # 4 字 + 消息固定开销 4
    assert ascii_ == 2 + 4
    with_calls = estimate_tokens(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c", "type": "function", "function": {"name": "ab", "arguments": "1234"}}
                ],
            }
        ]
    )
    assert with_calls == 1 + 1 + 4  # name 2 字符→1 + args 4 字符→1 + 开销 4


def test_token_meter_anchor_plus_increment(workdir: Path) -> None:
    """锚点 = 上次调用的真实 usage；增量 = 锚点 entry 之后的轨迹消息估算。"""
    traj = _step_traj(workdir, "meter.jsonl", n=2)
    meter = TokenMeter()
    full = meter.estimate(traj)
    assert full == estimate_tokens(
        [e.payload["message"] for e in traj.path() if e.type == MESSAGE]
    )

    meter.anchor(traj.leaf_id, 500)
    assert meter.estimate(traj) == 500  # 无新增 → 就是锚点值

    _user(traj, "你好世界")  # 4 字 + 开销 4
    assert meter.estimate(traj) == 500 + 8

    # 锚点被 rewind 掉：回退全量估算（不炸、不错账）
    first_user = traj.path()[1]
    meter.anchor(traj.leaf_id, 500)
    traj.branch(first_user.id)
    assert meter.estimate(traj) == estimate_tokens(
        [e.payload["message"] for e in traj.path() if e.type == MESSAGE]
    )


# ---------------------------------------------------------------- 水位


def test_trigger_tokens_ratio_and_reserve() -> None:
    ratio = CompactionPolicy(mode="ratio", window_tokens=1000, watermark=0.7)
    reserve = CompactionPolicy(mode="reserve", window_tokens=1000, reserve_tokens=200)
    assert trigger_tokens(ratio) == 700
    assert trigger_tokens(reserve) == 800
    assert trigger_tokens(CompactionPolicy()) is None  # window=0：计量不可用，不触发


# ---------------------------------------------------------------- 刀口


def test_cut_before_step_lands_on_nth_step_start(workdir: Path) -> None:
    traj = _step_traj(workdir, "cut.jsonl", n=3)
    starts = _step_starts(traj)
    assert cut_before_step(traj.path(), 1) == starts[-1]
    assert cut_before_step(traj.path(), 2) == starts[-2]
    # step 数不足 keep_steps：刀口推到最早的 step 起点（保留窗 step 数是上界）
    assert cut_before_step(traj.path(), 5) == starts[0]


def test_cut_before_step_tool_pairing_intact(workdir: Path) -> None:
    """刀口之后的保留窗以 step 起点开头，tool 结果跟在后面——配对天然完整。"""
    traj = _step_traj(workdir, "pair.jsonl", n=3)
    cut_id = cut_before_step(traj.path(), 1)
    entries = traj.path()
    cut = next(e for e in entries if e.id == cut_id)
    assert cut.payload["message"].get("tool_calls")  # 保留窗以 tool_call 发起开头
    after = entries[entries.index(cut) + 1 :]
    assert after[0].payload["message"]["role"] == "tool"  # 结果紧跟其后


def test_cut_before_step_token_budget_stops_earlier(workdir: Path) -> None:
    """keep_tokens 预算比 step 数先用尽时，刀口更早（双约束取更紧）。"""
    traj = _step_traj(workdir, "budget.jsonl", n=3)
    starts = _step_starts(traj)
    # 预算只够最近 1 个 step：在倒数第 2 个 step 起点处顶死 → 刀口 = starts[-1]
    per_step = estimate_tokens(
        [e.payload["message"] for e in traj.path() if e.type == MESSAGE]
    ) // 3
    assert cut_before_step(traj.path(), 3, keep_tokens=per_step + 1) == starts[-1]
    # 预算够 2 个 step：刀口 = starts[-2]
    assert cut_before_step(traj.path(), 3, keep_tokens=per_step * 2 + 1) == starts[-2]


def test_cut_before_step_pure_chat_falls_back_to_turn(workdir: Path) -> None:
    """纯聊天没有 step：退回 turn 刀口（保留最近 N 条真实 user）。"""
    traj = Trajectory.create(TrajectoryLog(workdir / "chat.jsonl"), sid="s1")
    _user(traj, "一")
    _answer(traj, "答一")
    _user(traj, "二")
    _answer(traj, "答二")
    starts = [
        e.id
        for e in traj.path()
        if e.type == MESSAGE
        and e.payload["message"].get("role") == "user"
        and not e.payload.get("synthetic")
    ]
    assert cut_before_step(traj.path(), 1) == starts[-1]
    # keep=2 与轮数相等：保留窗覆盖全部 turn，无可压段
    assert cut_before_step(traj.path(), 2) is None
    assert cut_before_step(traj.path(), 5) is None  # 轮数不足
    # 旧 turn 刀口仍是兜底实现的一部分
    assert cut_before_turn(traj.path(), 1) == starts[-1]


# ---------------------------------------------------------------- 折叠


def test_maybe_compact_folds_previous_summary(workdir: Path) -> None:
    """第二刀：摘要输入 = 上一刀摘要（previous）+ 两刀之间增量，原文不重读；
    投影认最后一切，旧摘要被吞。"""
    traj = _step_traj(workdir, "fold.jsonl", n=3)
    policy = CompactionPolicy(keep_steps=1)
    summarizer = ScriptedSummarizer(["摘要A", "摘要B"])

    first = asyncio.run(maybe_compact(traj, policy, summarizer, reason="manual"))
    assert first is not None
    assert first.payload["summary"] == "摘要A"
    assert first.payload["policy_version"] == policy.version
    # 第一刀的段：keep_steps=1 → 刀口在最后一个 step 起点，段 = 之前的全部消息
    assert summarizer.previous == [None]

    # 增量：第四轮一个完整 step
    _user(traj, "第四轮的问题")
    _call(traj, "c4", "query_inventory", '{"category": "品类4"}')
    _result(traj, "c4", "品类4 的查询结果")

    second = asyncio.run(maybe_compact(traj, policy, summarizer, reason="manual"))
    assert second is not None
    assert second.payload["summary"] == "摘要B"
    # previous 传递了上一刀摘要
    assert summarizer.previous == [None, "摘要A"]
    # 增量段只含两刀之间的消息（u4），不重读任何第一刀之前的原文
    assert [str(m.get("content")) for m in summarizer.segments[1]] == ["第四轮的问题"]

    # 投影认最后一切：[<摘要B>, 保留窗]（无 system：header 未配 prompt），摘要A 不在视图
    proj = build_context(traj)
    assert proj.messages[0]["role"] == "user"
    assert proj.messages[0]["content"] == "<summary>摘要B</summary>"
    assert not any("摘要A" in str(m.get("content")) for m in proj.messages)
    kept_roles = [m["role"] for m in proj.messages[1:]]
    assert kept_roles == ["assistant", "tool"]  # 保留窗 = 第二刀保留的那 1 个 step
    # 两条 compaction 都在轨迹里（append-only）
    assert [e.type for e in traj.entries()].count(COMPACTION) == 2


def test_projection_takes_last_cut_and_rewind_restores(workdir: Path) -> None:
    """手搓两刀的投影语义：认最后一切；rewind 回中间 = 回到上一刀的视图；
    rewind 过第一刀 = 压缩没发生过。"""
    traj = Trajectory.create(TrajectoryLog(workdir / "twocuts.jsonl"), sid="s1")
    _user(traj, "u1")
    a1 = _call(traj, "c1", "query_inventory", "{}")
    _result(traj, "c1", "r1")
    _user(traj, "u2")
    a2 = _call(traj, "c2", "query_inventory", "{}")
    _result(traj, "c2", "r2")
    u3 = _user(traj, "u3")
    comp_a = traj.append(
        COMPACTION, {"summary": "摘要A", "keep_from_id": a2.id, "reason": "manual"}
    )
    u4 = _user(traj, "u4")
    a4 = _call(traj, "c4", "query_inventory", "{}")
    _result(traj, "c4", "r4")
    comp_b = traj.append(
        COMPACTION, {"summary": "摘要B", "keep_from_id": a4.id, "reason": "manual"}
    )

    # 认最后一切：[<B>, a4, r4]（手搓轨迹没有 system prompt，header 为空）
    proj = build_context(traj)
    assert proj.messages[0]["content"] == "<summary>摘要B</summary>"
    assert [m["role"] for m in proj.messages[1:]] == ["assistant", "tool"]

    # rewind 到 compA：回到第一刀的视图 [<A>, a2, r2, u3]
    traj.branch(comp_a.id)
    proj_a = build_context(traj)
    assert proj_a.messages[0]["content"] == "<summary>摘要A</summary>"
    assert [m["role"] for m in proj_a.messages[1:]] == ["assistant", "tool", "user"]

    # rewind 过第一刀（到 u1）：压缩没发生过，旧消息逐字回来
    traj.branch(u3.id)
    proj_full = build_context(traj)
    assert not any(str(m.get("content", "")).startswith("<summary>") for m in proj_full.messages)
    assert any(m.get("content") == "u1" for m in proj_full.messages)
    assert [m["role"] for m in proj_full.messages] == ["user", "assistant", "tool"] * 2 + ["user"]


# ---------------------------------------------------------------- 复检


def _kept_tokens(traj: Trajectory, cut_id: str) -> int:
    entries = traj.path()
    cut = next(e for e in entries if e.id == cut_id)
    return estimate_tokens(
        [e.payload["message"] for e in entries[entries.index(cut) :] if e.type == MESSAGE]
    )


def test_recheck_shrinks_keep_window(workdir: Path) -> None:
    """复检不过 → 收缩保留窗重压；收到 1 个 step 仍不过 → 不硬压（返回 None）。"""
    traj = _step_traj(workdir, "recheck.jsonl", n=4)
    starts = _step_starts(traj)
    # 摘要 6 token；触发线卡在"keep=2 能过、keep=3 不能过"之间
    summary_tokens = 6
    kept3 = _kept_tokens(traj, cut_before_step(traj.path(), 3))
    kept2 = _kept_tokens(traj, cut_before_step(traj.path(), 2))
    trigger = summary_tokens + kept2 + 5  # > summary+kept2，< summary+kept3（kept3>kept2+5）
    assert summary_tokens + kept3 > trigger
    policy = CompactionPolicy(
        mode="ratio", window_tokens=1000, watermark=trigger / 1000, keep_steps=3
    )
    summarizer = ScriptedSummarizer(["摘要"])
    entry = asyncio.run(
        maybe_compact(traj, policy, summarizer, reason="watermark", tokens_now=trigger + 1)
    )
    assert entry is not None
    assert summarizer.calls >= 2  # 复检失败触发了收缩重压
    assert entry.payload["keep_from_id"] == starts[-2]  # 收缩到 keep=2


def test_recheck_never_fits_returns_none(workdir: Path) -> None:
    """摘要本身巨大：收到 1 个 step 仍超线 → 放弃（不硬压、不 append）。"""
    traj = _step_traj(workdir, "never.jsonl", n=3)
    policy = CompactionPolicy(mode="ratio", window_tokens=1000, watermark=0.7, keep_steps=2)
    summarizer = ScriptedSummarizer(["巨" * 5000])
    entry = asyncio.run(
        maybe_compact(traj, policy, summarizer, reason="watermark", tokens_now=900)
    )
    assert entry is None
    assert not [e for e in traj.entries() if e.type == COMPACTION]
    # 手动调用跳过复检：照压（宁压不挡）
    manual = asyncio.run(
        maybe_compact(traj, policy, ScriptedSummarizer(["x"]), reason="manual")
    )
    assert manual is not None


# ---------------------------------------------------------------- 水位门


def test_maybe_compact_watermark_gate(workdir: Path) -> None:
    """自动通道按水位门控；手动通道跳过水位。"""
    traj = _step_traj(workdir, "gate.jsonl", n=2)
    policy = CompactionPolicy(window_tokens=1000, watermark=0.7)
    summarizer = ScriptedSummarizer(["摘要"])
    # 未越线
    assert (
        asyncio.run(
            maybe_compact(traj, policy, summarizer, reason="watermark", tokens_now=500)
        )
        is None
    )
    # 没有计量（tokens_now=None）→ 不触发
    assert (
        asyncio.run(
            maybe_compact(traj, policy, summarizer, reason="watermark", tokens_now=None)
        )
        is None
    )
    # 越线 → 压缩，reason 落 entry
    entry = asyncio.run(
        maybe_compact(traj, policy, summarizer, reason="watermark", tokens_now=900)
    )
    assert entry is not None and entry.payload["reason"] == "watermark"
    # 手动：无视水位（此例即使 tokens_now 很低也压）
    traj2 = _step_traj(workdir, "gate2.jsonl", n=2)
    entry2 = asyncio.run(
        maybe_compact(traj2, policy, ScriptedSummarizer(["摘要"]), reason="manual", tokens_now=10)
    )
    assert entry2 is not None


# ---------------------------------------------------------------- 摘要组装


def test_serialize_segment_single_user_message() -> None:
    """摘要输入 = 单条 user 消息：对话在 <conversation> 里，指令收尾；
    折叠时上一刀摘要在 <previous-summary> 里。"""
    segment = [
        {"role": "user", "content": "问题"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "query_inventory", "arguments": '{"category": "保温杯"}'},
                }
            ],
        },
    ]
    first = serialize_segment(segment, previous_summary=None)
    assert first["role"] == "user"
    assert first["content"].count("<conversation>") == 1
    assert "[user] 问题" in first["content"]
    assert "query_inventory" in first["content"]  # 工具调用带参数可见
    assert first["content"].rstrip().endswith("输出摘要。")  # 指令收尾
    assert "previous-summary" not in first["content"]

    folded = serialize_segment(segment, previous_summary="上一刀")
    assert "<previous-summary>\n上一刀\n</previous-summary>" in folded["content"]
    assert folded["content"].index("<conversation>") < folded["content"].index("<previous-summary>")


def test_compression_system_matches_doc() -> None:
    """压缩 system prompt 与 05 章文档同一版本（五条规则，防重复执行）。"""
    assert "只压缩" in COMPRESSION_SYSTEM
    assert "关键事实与数据" in COMPRESSION_SYSTEM
    assert "工具调用" in COMPRESSION_SYSTEM
    assert "副作用" in COMPRESSION_SYSTEM
    assert "[INSUFFICIENT]" in COMPRESSION_SYSTEM


# ---------------------------------------------------------------- agent 集成


class ScriptedLLM:
    """最小脚本化 LLM（可注入 usage 块，供锚点计量测试）。"""

    def __init__(self, script: list[list[dict[str, Any]]], *, prompt_tokens: int = 0) -> None:
        self.script = script
        self.calls = 0
        self.contexts: list[list[dict[str, Any]]] = []
        self.prompt_tokens = prompt_tokens

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        self.contexts.append([dict(m) for m in messages])
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for chunk in self.script[idx]:
            yield chunk
        if self.prompt_tokens:
            yield {
                "type": "usage",
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": 1,
            }


def _tool_step(cid: str, name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": 0,
            "id": cid,
            "name": name,
            "args_delta": _json_dumps(args),
        }
    ]


def _json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def _text_step(text: str) -> list[dict[str, Any]]:
    return [{"type": "text_delta", "text": text}]


def _agent(bus: EventBus, store: SessionStore, llm: ScriptedLLM, **kw: Any) -> Agent:
    return Agent(bus, llm, store=store, system_prompt="SYS", **kw)


def test_agent_watermark_auto_compact(workdir: Path, data_copies: None) -> None:
    """水位自动压缩：step 边界计量越线 → 自动 maybe_compact（reason=watermark），
    下一次调用就是新视图；未越线的会话全程不压。"""
    log = EventLog(str(workdir / "events"))
    bus = EventBus(log)
    store = SessionStore(workdir / "sessions")
    # 每条消息估算 ~54 token（50 字 ASCII 不行——用 CJK：50 字 = 50 + 4）
    long_text = "查" * 50
    llm = ScriptedLLM(
        [
            _tool_step("c1", "query_inventory", {"category": "保温杯"}),
            _text_step("答一" + long_text),
            _tool_step("c2", "query_inventory", {"category": "玻璃杯"}),
            _text_step("答二" + long_text),
        ],
        prompt_tokens=0,
    )
    # 触发线 140：第二轮的第二个边界（150）越线
    policy = CompactionPolicy(window_tokens=200, watermark=0.7, keep_steps=1)
    agent = _agent(
        bus,
        store,
        llm,
        compaction_policy=policy,
        summarizer=ScriptedSummarizer(["摘要"]),
    )
    traj = store.start(cwd=str(workdir), system_prompt="SYS")
    agent.attach(traj)
    events: list[Event] = []
    ended = asyncio.Event()

    async def on_end(event: Event) -> None:
        ended.set()

    async def on_any(event: Event) -> None:
        events.append(event)

    bus.subscribe(Subscription("end", ("turn_end",), on_end, mode=OBSERVE))
    bus.subscribe(Subscription("any", ("*",), on_any, mode=OBSERVE))

    async def go() -> None:
        for text in ("第一轮查询", "第二轮查询"):
            bus.publish(Event("user_input", traj.sid, {"text": text}), to=agent.agent_id)
            await asyncio.wait_for(ended.wait(), 10)
            ended.clear()

    asyncio.run(asyncio.wait_for(go(), 10))

    compacted = [e for e in events if e.type == "context_compacted"]
    assert len(compacted) == 1
    assert compacted[0].payload["reason"] == "watermark"
    # 自动压缩后的调用：视图 = [system, <摘要>, 保留窗…]
    assert llm.contexts[-1][1]["content"].startswith("<summary>")
    # 恰好一条 compaction entry，原文都在
    assert [e.type for e in traj.entries()].count(COMPACTION) == 1


def test_agent_watermark_not_triggered_when_small(workdir: Path, data_copies: None) -> None:
    """小会话（token 远离水位）全程不自动压缩。"""
    log = EventLog(str(workdir / "events"))
    bus = EventBus(log)
    store = SessionStore(workdir / "sessions")
    llm = ScriptedLLM(
        [
            _tool_step("c1", "query_inventory", {"category": "保温杯"}),
            _text_step("保温杯有货"),
        ]
    )
    policy = CompactionPolicy(window_tokens=100000, watermark=0.7, keep_steps=1)
    agent = _agent(bus, store, llm, compaction_policy=policy)
    traj = store.start(cwd=str(workdir), system_prompt="SYS")
    agent.attach(traj)
    events: list[Event] = []
    ended = asyncio.Event()

    async def on_end(event: Event) -> None:
        ended.set()

    async def on_any(event: Event) -> None:
        events.append(event)

    bus.subscribe(Subscription("end", ("turn_end",), on_end, mode=OBSERVE))
    bus.subscribe(Subscription("any", ("*",), on_any, mode=OBSERVE))

    async def go() -> None:
        bus.publish(Event("user_input", traj.sid, {"text": "查一下保温杯"}), to=agent.agent_id)
        await asyncio.wait_for(ended.wait(), 10)

    asyncio.run(asyncio.wait_for(go(), 10))
    assert not [e for e in events if e.type in ("context_compacted", "context_compact_failed")]


def test_agent_anchors_usage_on_final_answer(workdir: Path) -> None:
    """usage 锚定覆盖最终答复步：无工具结果的最后一次调用（往往上下文最长）
    也要锚定——否则纯聊天会话永远没有锚点，计量退化成纯字符估算。"""
    log = EventLog(str(workdir / "events"))
    bus = EventBus(log)
    store = SessionStore(workdir / "sessions")
    llm = ScriptedLLM([_text_step("保温杯有货，库存 45 件。")], prompt_tokens=123)
    agent = _agent(bus, store, llm)
    traj = store.start(cwd=str(workdir), system_prompt="SYS")
    agent.attach(traj)
    ended = asyncio.Event()

    async def on_end(event: Event) -> None:
        ended.set()

    bus.subscribe(Subscription("end", ("turn_end",), on_end, mode=OBSERVE))

    async def go() -> None:
        bus.publish(Event("user_input", traj.sid, {"text": "查一下保温杯"}), to=agent.agent_id)
        await asyncio.wait_for(ended.wait(), 10)

    asyncio.run(asyncio.wait_for(go(), 10))
    meter = agent._meter(traj.sid)
    answer_est = estimate_tokens([{"role": "assistant", "content": "保温杯有货，库存 45 件。"}])
    assert meter.estimate(traj) == 123 + answer_est
    asyncio.run(agent.stop())


def test_agent_max_steps_configurable(workdir: Path) -> None:
    """MAX_STEPS 提为可配置：默认 20；剧本工具循环按配置的步数封顶。"""
    log = EventLog(str(workdir / "events"))
    bus = EventBus(log)
    store = SessionStore(workdir / "sessions")
    endless = [_tool_step(f"c{i}", "query_inventory", {"category": "保温杯"}) for i in range(50)]
    llm = ScriptedLLM([endless[0]] * 50)
    agent = _agent(bus, store, llm, max_steps=2)
    assert agent.max_steps == 2
    traj = store.start(cwd=str(workdir), system_prompt="SYS")
    agent.attach(traj)
    ends: list[Event] = []
    ended = asyncio.Event()

    async def on_end(event: Event) -> None:
        ends.append(event)
        ended.set()

    bus.subscribe(Subscription("end", ("turn_end",), on_end, mode=OBSERVE))

    async def go() -> None:
        bus.publish(Event("user_input", traj.sid, {"text": "一直查"}), to=agent.agent_id)
        await asyncio.wait_for(ended.wait(), 10)

    asyncio.run(asyncio.wait_for(go(), 10))
    assert llm.calls == 2
    assert ends[0].payload["reason"] == "max steps"
    # 默认值：不再是最初的 4
    default_agent = _agent(bus, store, ScriptedLLM([[_text_step("ok")]]))
    assert default_agent.max_steps == 20
    asyncio.run(agent.stop())
    asyncio.run(default_agent.stop())
