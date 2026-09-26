"""手动压缩：Summarizer 协议 + 刀口计算 + maybe_compact。

压缩是视图标记，不是对数据的手术：往树上追加一个 compaction entry
（summary + keep_from_id），投影（agent.build_context）按 5a 钉死的语义读它——
刀口前跳过、摘要插视图最前、认第一刀。本模块只管三件事：

1. **刀口在哪**：cut_before_turn——保留最近 N 轮，刀口落在倒数第 N 轮的
   第一条真实 user entry 上。轮的起点 = 非合成的 user message（redirect /
   打断占位不算开轮）。刀口天然落在 turn 边界：保留段以一条 user 开头，
   tool 配对完整，不用再修序列。
2. **摘要从哪来**：Summarizer 协议两档——ScriptedSummarizer（离线确定性，
   文本是剧本的一部分，可含刻意遗漏：压缩的代价要真实可感）/
   LiveSummarizer（裸 chat：不带工具、max_tokens 封顶、渲染成文本喂进去，
   只压缩不推理不补没发生过的事）。
3. **怎么落**：maybe_compact——拒绝第二刀（多次压缩的滚动折叠、投影认最后
   一刀，归 05；在那之前触发第二次压缩，其摘要会被投影跳过）。

摘要的输入是刀口之前的投影视图消息（agent 侧给 prefix_view）：
branch_summary 的 <summary> 也在里面——"摘要吞摘要"在手动档就第一次发生。
自动触发的策略（水位检测、按 token 计量、滚动折叠、兜底阶梯）归 05；
本章的触发形状（compact_request 控制事件、step 边界生效）在 agent 侧。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .trajectory import BRANCH_SUMMARY, COMPACTION, MESSAGE, Entry, Trajectory

Args = dict[str, Any]


@dataclass(frozen=True)
class CompactionPolicy:
    """手动档的策略常数。水位、自适应保留窗、policy_version 不变式归 05。"""

    version: str = "stage04.manual.v1"
    keep_turns: int = 2  # 保留最近 N 轮原文


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

    async def summarize(
        self, segment: list[dict[str, Any]], previous: str | None = None
    ) -> str:
        self.segments.append(segment)
        idx = min(self.calls, len(self.texts) - 1)
        self.calls += 1
        return self.texts[idx]


class LiveSummarizer:
    """真模型摘要：裸 chat——不带工具、max_tokens 封顶。

    摘要调用不是 agent loop 的一步：它读被压段、写一段文本，不需要工具，
    也不该被工具分心。对话渲染成一段文本喂进去（不作为 chat 消息序列），
    孤儿工具结果也读得懂。max_tokens 封顶：摘要写太长，压了等于没压。
    """

    def __init__(self, llm: Any, *, max_tokens: int = 512) -> None:
        self.llm = llm
        self.max_tokens = max_tokens

    async def summarize(
        self, segment: list[dict[str, Any]], previous: str | None = None
    ) -> str:
        system = (
            "你是会话压缩器。把给到的对话压成一份摘要，供 agent 之后继续任务用。"
            "只压缩、不推理、不补没发生过的事。按四段输出："
            "【已完成】【关键事实与规则】【副作用】【待办】。"
        )
        lines = []
        if previous:
            lines.append(f"上一刀的摘要（新摘要要吞掉它）：\n{previous}")
        lines.append("被压缩的对话：")
        for m in segment:
            lines.append(f"[{m.get('role')}] {_render(m)}")
        lines.append("输出摘要。")
        out: list[str] = []
        async for chunk in self.llm.stream_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}],
            tools=None,
            max_tokens=self.max_tokens,
        ):
            if chunk["type"] == "text_delta":
                out.append(chunk["text"])
        return "".join(out).strip()


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


def cut_before_turn(entries: list[Entry], keep_turns: int) -> str | None:
    """刀口 = 倒数第 keep_turns 轮的第一条真实 user entry 的 id。

    保留段以一条 user 开头：tool 配对完整，天然满足消息序列约束。
    轮数不足（没有可压的段）返回 None。
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


async def maybe_compact(
    traj: Trajectory,
    summarizer: Summarizer,
    *,
    keep_turns: int = 2,
    reason: str = "manual",
    prefix_view: Callable[[Trajectory, str], list[dict[str, Any]]],
) -> Entry | None:
    """算刀口 → 摘要 → 追加 compaction entry。返回 entry；不可压返回 None。

    - 已有压缩在路径上：拒绝第二刀（滚动折叠归 05，在那之前第二次压缩的
      摘要会被投影跳过——宁可不压，也不做语义没定义的事）；
    - 轮数不足：没有可压的段；
    - 摘要器抛错：原样上抛，调用方（agent）决定 fail-open 怎么留痕。

    摘要的输入 = prefix_view(traj, keep_from_id)：刀口之前的投影视图消息。
    """
    entries = traj.path()
    if any(e.type == COMPACTION for e in entries):
        return None
    keep_from_id = cut_before_turn(entries, keep_turns)
    if keep_from_id is None:
        return None
    segment = prefix_view(traj, keep_from_id)
    summary = await summarizer.summarize(segment, previous=None)
    return traj.append(
        COMPACTION,
        {"summary": summary, "keep_from_id": keep_from_id, "reason": reason},
    )
