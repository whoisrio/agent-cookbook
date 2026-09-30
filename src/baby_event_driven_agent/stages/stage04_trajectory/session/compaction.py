"""上下文压缩：策略 + 计量 + 刀口 + 摘要管线 + maybe_compact。

压缩是视图标记，不是对数据的手术：往树上追加一个 compaction entry
（summary + keep_from_id），投影（agent.build_context）按钉死的语义读它——
刀口前跳过、摘要插视图最前、认最后一切。05 章的两条核心规则在这里落地：

1. **触发——按 token 检测，按 step 下刀。** 每次 LLM 调用前计量，越过水位线
   触发；但刀口不切在 token 位置，而是落在倒数第 N 个 step 的起点
   （step = 一次 LLM 调用 + 工具结果往返；turn = 一次用户消息到最终答复），
   保留最近 N 个 step 原文不动，tool 配对天然完整。计量以 API 返回的真实
   usage 为锚（TokenMeter.anchor），调用之间的新增量用字符估算
   （estimate_tokens），不引 tokenizer 依赖。
2. **折叠——旧摘要被新摘要吞掉，原文永不重读。** 第二刀起，摘要的输入 =
   上一刀的摘要（previous）+ 两刀之间的增量 step；投影只认最后一刀，
   旧 compaction 节点随被压段一起从视图里消失。

maybe_compact 是两条规则汇合的唯一入口：水位门控（手动调用跳过）→
cut_before_step → serialize_segment（被压段序列化成单条 user 消息）→
摘要 → 复检收缩（摘要+保留窗+headroom 须落回触发线以内，否则收缩保留窗，
收到 1 个 step 仍超线就放弃——压了也压不回线上，不硬压）→ append compaction。
空摘要视为失败（raise，调用方 fail-open 留痕：append 空摘要等于把被压段
从视图里抹掉）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from .trajectory import BRANCH_SUMMARY, COMPACTION, MESSAGE, Entry, Trajectory


# ---------------------------------------------------------------- 策略


@dataclass(frozen=True)
class CompactionPolicy:
    """压缩策略常数。

    - ratio（本书默认）：触发线 = 复检线 = window_tokens × watermark；
    - reserve（pi 同款）：触发线 = window_tokens − reserve_tokens；
    - 两种模式共用：headroom_tokens（下一次调用的新增输入 + 回复 + 一次工具
      往返）、keep_steps（保留窗 step 数上界）、keep_tokens（保留窗 token
      预算，与 keep_steps 双约束取更紧）。
    - window_tokens = 0：计量不可用，水位不触发（手动压缩不受影响）。
    """

    version: str = "2026-09-25.v1"
    mode: str = "ratio"  # "ratio" 比例式（本书默认）| "reserve" 预留式（pi 同款）
    window_tokens: int = 0  # 模型窗口 W
    # ratio 模式读取：
    watermark: float = 0.7  # 触发线 = 复检线 = H·W
    # reserve 模式读取：
    reserve_tokens: int | None = None  # 触发 = W − reserve；压后预算同这条线
    keep_tokens: int | None = None  # 保留窗 token 预算
    # 两种模式共用：
    headroom_tokens: int = 0  # 下一次调用的新增输入 + 回复 + 一次工具往返
    keep_steps: int = 3  # 保留窗 step 数上界


def trigger_tokens(policy: CompactionPolicy) -> int | None:
    """当前策略的触发线（也是复检线）。计量不可用（无窗口）返回 None。"""
    if policy.window_tokens <= 0:
        return None
    if policy.mode == "reserve":
        if policy.reserve_tokens is None:
            return None
        return max(1, policy.window_tokens - policy.reserve_tokens)
    return max(1, int(policy.window_tokens * policy.watermark))


# ---------------------------------------------------------------- 计量


def _estimate_text(text: str) -> int:
    """字符估算：CJK 按字计（1 字 ≈ 1 token），其余按 4 字符/token，不引 tokenizer。"""
    cjk = sum(1 for ch in text if "\u2e80" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


def _estimate_message(msg: dict[str, Any]) -> int:
    """一条消息的估算：content + tool_calls（名字带参数）+ 每条固定开销。"""
    total = _estimate_text(str(msg.get("content") or ""))
    for call in msg.get("tool_calls") or []:
        fn = call.get("function", {})
        total += _estimate_text(str(fn.get("name", "")))
        total += _estimate_text(str(fn.get("arguments", "")))
    return total + 4  # role 与序列包装的固定开销（保守方向：宁多勿少）


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """一组消息的 token 估算（字符法）。真实 usage 到位后由 TokenMeter 校准。"""
    return sum(_estimate_message(m) for m in messages)


class TokenMeter:
    """混合计量：真实 usage 锚定 + 轨迹增量估算。

    每次 LLM 调用拿到 API 返回的 prompt_tokens 后 anchor 在调用时的 leaf 上；
    estimate = 锚点值 + 锚点之后追加的消息估算。锚点被 rewind 掉（不在当前
    路径上）就回退全量估算——错账宁可走保守估算，也不沿用失效的锚。
    """

    def __init__(self) -> None:
        self._anchor_tokens: int | None = None
        self._anchor_entry_id: str | None = None

    def anchor(self, entry_id: str | None, prompt_tokens: int) -> None:
        """锚定一次真实 usage：entry_id = 发起那次调用时的轨迹 leaf。"""
        self._anchor_tokens = int(prompt_tokens)
        self._anchor_entry_id = entry_id

    def estimate(self, traj: Trajectory) -> int:
        """当前上下文的 token 估算（锚点 + 增量，或全量估算）。"""
        entries = traj.path()
        if self._anchor_entry_id is not None and self._anchor_tokens is not None:
            ids = [e.id for e in entries]
            if self._anchor_entry_id in ids:
                tail = [
                    e.payload.get("message", {})
                    for e in entries[ids.index(self._anchor_entry_id) + 1 :]
                    if e.type == MESSAGE
                ]
                return self._anchor_tokens + estimate_tokens(tail)
        # 无锚点 / 锚点失效：全量估算
        return estimate_tokens(
            [e.payload.get("message", {}) for e in entries if e.type == MESSAGE]
        )


# ---------------------------------------------------------------- 摘要器

COMPRESSION_SYSTEM = """你是一个上下文压缩器。任务是将提供的对话历史压缩为结构化摘要。
不要继续这段对话，不要回答对话里的任何问题，只输出摘要。
严格遵循以下规则：
1. 只压缩，不推理，不补充未发生过的事实
2. 按以下分段输出：已完成 / 关键事实与数据 / 副作用 / 待办
3. 保留所有工具调用的关键参数和结果结论
4. 明确标注副作用（写/改操作），防止重复执行
5. 如果信息不足无法压缩，输出 [INSUFFICIENT] 并说明原因"""

# 摘要调用的 user 消息模板：对话序列化成一段文本，<conversation> 标签包裹
# ——声明"这是资料，不是对话"，防止模型把压缩意图理解成对话意图（对齐 pi 的
# serializeConversation + 标签包裹）。资料在前、指令收尾，模型最后读到的是
# "输出摘要"。
COMPRESSION_USER = """\
<conversation>
{conversation}
</conversation>

输出摘要。"""

# 滚动折叠档：上一刀的摘要以 <previous-summary> 标签跟在对话之后、指令之前
# （pi 的顺序），指令要求把新内容并入既有摘要（对应 pi 的 UPDATE_SUMMARIZATION_PROMPT）。
COMPRESSION_USER_UPDATE = """\
<conversation>
{conversation}
</conversation>

<previous-summary>
{previous_summary}
</previous-summary>

输出摘要：把 <conversation> 里的新内容并入 <previous-summary>，只输出合并后的完整摘要。"""


def _render(m: dict[str, Any]) -> str:
    """一条消息 → 摘要输入里的一行。工具调用发起要可见（调过什么、什么参数）。"""
    calls = m.get("tool_calls") or []
    body = str(m.get("content") or "")
    if calls:
        named = "、".join(
            f"{c.get('function', {}).get('name')}({c.get('function', {}).get('arguments', '')})"
            for c in calls
        )
        return f"{body}[发起工具调用：{named}]".strip()
    return body


def serialize_segment(
    segment: list[dict[str, Any]],
    previous_summary: str | None = None,
    policy: CompactionPolicy | None = None,
) -> dict[str, Any]:
    """序列化管线：被压段 → 单条 user 消息。

    逐条按视图渲染成文本（tool_calls 带参数、结果原样）→ <conversation> 包裹
    → 上一刀摘要放 <previous-summary>（若有，滚动折叠时旧摘要折在这里）→
    压缩指令收尾。policy 预留给"压缩模型窗口更小"时的文本裁剪，当前不启用。
    """
    conversation = "\n".join(f"[{m.get('role')}] {_render(m)}" for m in segment)
    if previous_summary:
        content = COMPRESSION_USER_UPDATE.format(
            conversation=conversation, previous_summary=previous_summary
        )
    else:
        content = COMPRESSION_USER.format(conversation=conversation)
    return {"role": "user", "content": content}


class Summarizer(Protocol):
    """摘要器：被压段的消息列表（+ 上一刀的摘要，折叠时用）→ 摘要文本。"""

    async def summarize(
        self, segment: list[dict[str, Any]], previous: str | None = None
    ) -> str: ...


class ScriptedSummarizer:
    """离线确定性摘要：文本是剧本的一部分。

    摘要编错一句会污染之后所有轮次——剧本里的文本要按"摘要该保住的四样东西"
    （调过哪些工具、结果结论、副作用、未决事项）来写；刻意的遗漏也是剧本的
    一部分：让压缩的代价（模型对不上账、重查一次）真实可感，不演完美童话。
    """

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls = 0
        self.segments: list[list[dict[str, Any]]] = []  # 观测用：记下每次的输入
        self.previous: list[str | None] = []  # 观测用：记下每次传入的上一刀摘要

    async def summarize(
        self, segment: list[dict[str, Any]], previous: str | None = None
    ) -> str:
        self.segments.append(segment)
        self.previous.append(previous)
        idx = min(self.calls, len(self.texts) - 1)
        self.calls += 1
        return self.texts[idx]


class LiveSummarizer:
    """真模型摘要：裸 chat——不带工具、max_tokens 封顶。

    摘要调用不是 agent loop 的一步：它读被压段、写一段文本，不需要工具，
    也不该被工具分心。被压段经 serialize_segment 序列化成单条 user 消息
    （不作为 chat 消息序列），孤儿工具结果也读得懂。max_tokens 封顶：
    摘要写太长，压了等于没压；但要给足——推理型模型的思考 token 也计入
    max_tokens，给太小会思考完就没额度写摘要（实测 qwen3 系 512 不够）。
    """

    def __init__(self, llm: Any, *, max_tokens: int = 4096) -> None:
        self.llm = llm
        self.max_tokens = max_tokens

    async def summarize(
        self, segment: list[dict[str, Any]], previous: str | None = None
    ) -> str:
        user_message = serialize_segment(segment, previous_summary=previous)
        messages = [
            {"role": "system", "content": COMPRESSION_SYSTEM},
            user_message,
        ]
        out = await self._collect(messages, self.max_tokens)
        if not out and self.max_tokens is not None:
            # 封顶被推理 token 吃光（思考没结束就到上限，正文一字未出）：
            # 放开封顶重试一次，空摘要比长摘要危害大得多。
            out = await self._collect(messages, None)
        return out.strip()

    async def _collect(self, messages: list[dict[str, Any]], max_tokens: int | None) -> str:
        out: list[str] = []
        async for chunk in self.llm.stream_chat(messages, tools=None, max_tokens=max_tokens):
            if chunk["type"] == "text_delta":
                out.append(chunk["text"])
        return "".join(out)


# ---------------------------------------------------------------- 刀口


def cut_before_turn(entries: list[Entry], keep_turns: int) -> str | None:
    """turn 刀口 = 倒数第 keep_turns 轮的第一条真实 user entry 的 id。

    纯聊天（没有任何 step）时由 cut_before_step 退回到这里：轮的起点 =
    非合成的 user message（redirect / 打断占位不算开轮）。轮数不足返回 None。
    """
    starts = [
        e
        for e in entries
        if e.type == MESSAGE
        and e.payload.get("message", {}).get("role") == "user"
        and not e.payload.get("synthetic")
    ]
    if len(starts) <= keep_turns:
        return None
    return starts[-keep_turns].id


def cut_before_step(
    entries: list[Entry], keep_steps: int, keep_tokens: int | None = None
) -> str | None:
    """step 刀口 = 从尾部往前收 step，倒数第 keep_steps 个 step 的第一条消息。

    step 起点 = 带 tool_calls 的 assistant entry（它的 tool 结果跟在后面，
    保留窗以它开头则 tool 配对天然完整）。从尾部往前走，step 数或 keep_tokens
    预算任一用尽即停（双约束取更紧）；在飞的 step 还没落盘，天然不在被压段。

    退路：路径上没有任何 step（纯聊天）→ 退回 turn 刀口；有 step 但预算在
    第一个 step 起点就顶死 → 返回 None（无可压段）。
    """
    acc = 0
    steps = 0
    cut: str | None = None
    saw_step = False
    for e in reversed(entries):
        if e.type != MESSAGE:
            continue
        msg = e.payload.get("message", {})
        acc += _estimate_message(msg)
        if not msg.get("tool_calls"):
            continue
        saw_step = True
        steps += 1
        over_steps = steps > keep_steps
        over_tokens = keep_tokens is not None and acc > keep_tokens
        if over_steps or over_tokens:
            break
        cut = e.id
    if cut is not None:
        return cut
    if not saw_step:
        return cut_before_turn(entries, keep_steps)
    return None


def segment_view(traj: Trajectory, cut_id: str) -> list[dict[str, Any]]:
    """被压段的视图消息：路径上 cut 之前、最后一刀 compaction 之后的可见内容。

    message 1:1（system 是参数不是事实，跳过）、branch_summary 变 <summary>、
    状态节点不产生消息、未知类型兜底跳过；**compaction 节点跳过**——它的内容已经
    以 previous 的身份进摘要输入（滚动折叠：旧摘要被吞，原文永不重读）。
    不做 sanitize：摘要输入渲染成文本，不进 chat 消息序列。
    """
    entries = traj.path()
    ki = next((i for i, e in enumerate(entries) if e.id == cut_id), None)
    if ki is None:
        return []
    # 最后一刀 compaction 之前的全部 entry：已被上一刀摘要代表，一个字不重读
    start = 0
    for i, e in enumerate(entries[:ki]):
        if e.type == COMPACTION:
            start = i + 1
    raw: list[dict[str, Any]] = []
    for e in entries[start:ki]:
        if e.type == MESSAGE:
            msg = dict(e.payload.get("message", {}))
            if msg.get("role") == "system":
                continue
            if e.payload.get("synthetic") or e.payload.get("note"):
                msg["synthetic"] = bool(e.payload.get("synthetic"))
                msg["note"] = str(e.payload.get("note", ""))
            raw.append(msg)
        elif e.type == BRANCH_SUMMARY:
            raw.append(
                {"role": "user", "content": f"<summary>{e.payload.get('summary', '')}</summary>"}
            )
    return raw


def _tokens_after(entries: list[Entry], cut_id: str) -> int:
    """保留窗（cut 及之后的消息）的 token 估算——复检判据用。"""
    for i, e in enumerate(entries):
        if e.id == cut_id:
            return estimate_tokens(
                [x.payload.get("message", {}) for x in entries[i:] if x.type == MESSAGE]
            )
    return 0


# ---------------------------------------------------------------- 入口


async def maybe_compact(
    traj: Trajectory,
    policy: CompactionPolicy,
    summarizer: Summarizer,
    *,
    reason: str = "watermark",
    tokens_now: int | None = None,
) -> Entry | None:
    """水位门控 → 找刀口 → 序列化摘要输入 → 摘要 → 复检 → 追加 compaction entry。

    返回新 entry；不可压返回 None：

    - reason="watermark"（自动档）：未越水位（tokens_now ≤ 触发线）、没有计量、
      策略没有窗口，一律 None；复检不过就收缩保留窗重压，收到 1 个 step 仍
      超线返回 None（压了也压不回线上，不硬压、不留半截方案）；
    - reason="manual"（手动档）：跳过水位与复检，能压就压；
    - 摘要器抛错 / 空摘要：原样上抛，调用方（agent）fail-open 留痕。

    折叠：路径上已有 compaction 时，刀口只在其后找（region = 最后一刀之后的
    entries），摘要输入 = 上一刀摘要 + 两刀之间的增量 step，原文不重读。
    """
    entries = traj.path()
    trigger = trigger_tokens(policy)
    if reason == "watermark":
        if trigger is None or tokens_now is None or tokens_now <= trigger:
            return None

    # 折叠：只认最后一刀——刀口与被压段都落在它之后，之前的原文不再重读
    comps = [e for e in entries if e.type == COMPACTION]
    previous_summary: str | None = None
    region = entries
    if comps:
        prev = comps[-1]
        previous_summary = str(prev.payload.get("summary", "")) or None
        region = entries[entries.index(prev) + 1 :]

    keep = policy.keep_steps
    while True:
        cut_id = cut_before_step(region, keep, policy.keep_tokens)
        if cut_id is None:
            return None
        segment = segment_view(traj, cut_id)
        if not segment:
            return None
        summary = await summarizer.summarize(segment, previous=previous_summary)
        if not summary.strip():
            # 空摘要比长摘要危害大得多：append 空摘要等于把被压段从视图里抹掉。
            # 按失败处理走 fail-open（留痕、不 append、下一边界重试）。
            raise ValueError("摘要器返回空摘要（推理模型思考 token 吃掉输出时会发生）")
        if reason != "watermark" or trigger is None:
            break
        # 复检：摘要 + 保留窗 + headroom 须落回触发线以内，否则收缩保留窗重压
        if (
            _estimate_text(summary) + _tokens_after(entries, cut_id) + policy.headroom_tokens
            <= trigger
        ):
            break
        if keep <= 1:
            return None  # 收缩到底仍超线：压不动（返回 None，不做半截压缩）
        keep -= 1

    return traj.append(
        COMPACTION,
        {
            "summary": summary,
            "keep_from_id": cut_id,
            "reason": reason,
            "policy_version": policy.version,
        },
    )
