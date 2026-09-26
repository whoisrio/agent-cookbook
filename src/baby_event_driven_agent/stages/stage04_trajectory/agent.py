"""agent：stage04 的全部能力（收件箱 / steering / 打断 / 转向 / 治理 / 人工确认），
history 换成轨迹层。

本章 agent 侧的改动只有三处，都很小——这正是把机制放在轨迹层上的意义：

1. `self.history: dict[sid, list]` → `self.trajectories: dict[sid, Trajectory]`。
   所有 `history.append(...)` 换成 `traj.append(MESSAGE, message_payload(...))`：
   消息进 append-only 的 entry 树（认父不认子），而不是内存 list。
2. `_step` 的上下文从内存 list 换成 `build_context(traj)`——
   history 降格为投影，每次 LLM 调用前从轨迹算出来。同一份轨迹文件
   逐字节可复现（eval 的地基）。build 是 agent 的活：轨迹层只管事实和树。
3. 合成消息（打断占位、封口占位、新消息）照旧进事实层
   （`synthetic: true` + note），只是落点从 history 变成轨迹——否则
   "history 是 log 的投影"在合成消息这条路上断掉。

sid 由 SessionStore 分配（不是调用方随口给）：agent 只认 store 里 start/resume
过的会话，`attach(traj)` 之后收工。中断 / steering 的逻辑与 stage03 同步：
收尾一律补 assistant 占位（文本按尾部角色选），打断可附一条新消息（turn 不结束）；
被掐的 step 不留半截消息这条 Stage 3 纪律，在树上同样成立——append 只发生在
step 成功结算之后。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from .transport.bus import EventBus
from .transport.events import (
    ALLOW,
    ASK,
    DENY,
    FOLLOWUP,
    MODIFY,
    STEERING,
    Decision,
    Event,
    UserMessage,
)
from .tools import TOOLS, Tool, build_system_prompt
from .session.compaction import LiveSummarizer, Summarizer, maybe_compact
from .session.store import SessionStore
from .session.trajectory import (
    BRANCH_SUMMARY,
    COMPACTION,
    MESSAGE,
    METADATA_TYPES,
    MODEL_CHANGE,
    PROMPT_CHANGE,
    Trajectory,
    message_payload,
)

logger = logging.getLogger(__name__)

MAX_STEPS = 4

# 中断收尾用的占位（同 stage03，自描述是硬要求）。
# 收尾一律补 assistant 占位，只有文本随尾部角色变：
STOP_CLOSER = "[本轮已被用户中断，不再基于上面的工具结果作答]"  # 尾部 tool：结果没人接
INTERRUPTED = "[response interrupted]"  # 尾部 user：声明这轮被打断，封死重答
NO_EXEC = "[被用户中断，未执行]"
# 治理否决的占位：同样要自描述，模型得知道这不是工具的输出
BLOCKED_PREFIX = "[被规则拦截，未执行]"
# 人工确认的结局，各自一个自描述占位：拒绝、超时（fail-closed）、被中断放弃
APPROVAL_REJECTED = "[人工确认未通过，未授权执行]"
APPROVAL_TIMEOUT = "[人工确认超时，未授权执行]"
APPROVAL_ABANDONED = "[等待人工确认期间被中断，未授权执行]"
# 超时那条裁决的署名：占位和 log 里都靠它把"人拒了"和"没人答"分开
APPROVAL_TIMEOUT_BY = "approval_timeout"

SYSTEM_PROMPT = build_system_prompt()


class _StepAborted(Exception):
    """流里检测到待消费中断，主动中止本轮流式消费（不等 cancel 时序生效）。

    on_interrupt 同步置位 _pending_interrupt 后才调 task.cancel()；协作式 cancel
    真正生效前，模型流里可能还残几个增量被渲染出来、落到 stop 行之后。这里在
    _step 循环顶部直接 raise，保证 stop 之后绝不再发任何 LLM 增量。"""

# 悬挂工具调用的占位：自描述是硬要求——模型得知道这不是工具的真实输出
UNKNOWN_TOOL_RESULT = "[UNKNOWN: 会话在工具执行前中断，结果缺失]"


@dataclass(frozen=True)
class Projection:
    """build_context 的产出：喂给模型的 messages + 覆盖式提取出的状态 + 投影统计。"""

    messages: list[dict[str, Any]]
    model: str | None
    stats: dict[str, int]


def build_context(traj: Trajectory) -> Projection:
    """轨迹 → messages。三步：路径遍历 → 按类型分派 → sanitize 收口。

    这是 agent 侧的活：轨迹层只管事实和树操作，怎么 build 上下文由 agent 决定。
    system prompt 全从轨迹来：初始值在 header，变更以 prompt_change entry
    追加（attach 时发现不一致就落盘），覆盖式提取、路径上最后一次生效、
    没有变更回落 header——和 model_change 同一个模式。不进消息序列，
    只决定前置的那条 system。
    纯函数：同一份轨迹文件，两次投影逐字节相同（测试钉死）。
    """
    entries = traj.path()
    stats = {"path": len(entries), "skipped": 0, "repaired": 0}

    # 压缩口子：路径上的 compaction 决定"摘要 + 跳过区间"。
    # 切割点不在路径上（比如被 rewind 掉）时，压缩节点当没发生过——
    # 压缩是当前路径上的视图，不是对数据的手术。
    # 只认第一条：多条的折叠语义归 5b（新摘要吞旧摘要、投影取最后一刀），
    # 在那之前不要触发第二次压缩——后序 compaction 的摘要会被跳过。
    # 完整语义见 session/trajectory.py 模块 docstring 的"压缩视图"一节。
    comp = next((e for e in entries if e.type == COMPACTION), None)
    summary_msg: dict[str, Any] | None = None
    kept_ids: set[str] | None = None
    if comp is not None:
        keep_from = str(comp.payload.get("keep_from_id", ""))
        ids = [e.id for e in entries]
        ci = ids.index(comp.id)
        if keep_from in ids[:ci]:
            ki = ids.index(keep_from)
            kept_ids = {e.id for e in entries[ki:]}
            summary_msg = {
                "role": "user",
                "content": f"<summary>{comp.payload.get('summary', '')}</summary>",
            }
        # keep_from 不在路径上：comp 什么都不做（按元数据跳过）

    model: str | None = None
    prompt: str | None = None
    raw: list[dict[str, Any]] = []
    for e in entries:
        if kept_ids is not None:
            if e.id == comp.id:  # type: ignore[union-attr]
                # 摘要插在最前面（pi 语义：CompactionSummaryMessage 开头），
                # 其后才是切割点之后保留的消息
                raw.insert(0, summary_msg)  # type: ignore[arg-type]
                continue
            if e.id not in kept_ids:
                stats["skipped"] += 1
                continue
        if e.type == MESSAGE:
            msg = dict(e.payload.get("message", {}))
            if e.payload.get("synthetic") or e.payload.get("note"):
                msg["synthetic"] = bool(e.payload.get("synthetic"))
                msg["note"] = str(e.payload.get("note", ""))
            raw.append(msg)
        elif e.type == MODEL_CHANGE:
            mid = str(e.payload.get("model_id", ""))
            if mid:
                model = mid  # 覆盖式提取：路径上最后一次生效
        elif e.type == PROMPT_CHANGE:
            p_ = str(e.payload.get("system_prompt", ""))
            if p_:
                prompt = p_  # 同上：prompt 变更是改状态事实
        elif e.type == BRANCH_SUMMARY:
            raw.append(
                {"role": "user", "content": f"<summary>{e.payload.get('summary', '')}</summary>"}
            )
        elif e.type == COMPACTION:
            stats["skipped"] += 1  # 没有切割点的 compaction：跳过
        elif e.type in METADATA_TYPES:
            stats["skipped"] += 1

    if prompt is None:
        prompt = str(traj.header.get("system_prompt", ""))  # 没变更过：回落 header
    messages = _sanitize(raw, prompt, stats)
    return Projection(messages=messages, model=model, stats=stats)


def _sanitize(
    raw: list[dict[str, Any]], system_prompt: str, stats: dict[str, int]
) -> list[dict[str, Any]]:
    """发给 provider 前的收口：保证消息序列约束永远满足。

    纪律：**只改投影，不改事实层**（测试钉死原文件字节不变）。
    悬挂的工具调用（assistant 要了结果、结果没来）补一条自描述占位——
    这是投影的内置默认行为，不是旋钮：诚实档让模型知道缺了什么、能自己
    决定重调。另一种做法是连 assistant 一起撤（pi 的 drop），在本项目
    没有真实需求前不外露成参数。
    """
    out: list[dict[str, Any]] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    i = 0
    while i < len(raw):
        m = raw[i]
        role = m.get("role")
        if role == "system":  # system 不该在轨迹里（它是参数），投影层丢掉
            i += 1
            continue
        if role == "tool":
            i += 1
            continue  # tool 结果只在下面的配对循环里收，落单的=孤儿，跳过
        # 合成标记只是轨迹注脚，任何角色上都要 strip 掉再发
        m.pop("synthetic", None)
        m.pop("note", None)
        calls = m.get("tool_calls") or []
        if calls:
            mark = len(out)
            out.append(dict(m))
            answered: set[str] = set()
            j = i + 1
            while j < len(raw) and raw[j].get("role") == "tool":
                if str(raw[j].get("tool_call_id")) in {c["id"] for c in calls}:
                    answered.add(str(raw[j].get("tool_call_id")))
                    out.append(dict(raw[j]))
                j += 1  # 配不上的 tool：孤儿，跳过
            missing = [c for c in calls if c["id"] not in answered]
            if missing:
                stats["repaired"] += len(missing)
                for c in missing:
                    out.append(
                        {"role": "tool", "tool_call_id": c["id"], "content": UNKNOWN_TOOL_RESULT}
                    )
            i = j
            continue
        out.append(dict(m))
        i += 1
    return out


class LLMClient(Protocol):
    def stream_chat(
        self, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]: ...


class Agent:
    def __init__(
        self,
        bus: EventBus,
        llm: LLMClient,
        agent_id: str = "agent",
        *,
        store: SessionStore,
        approval_timeout: float = 30.0,
        system_prompt: str | None = None,
        tools: dict[str, Tool] | None = None,
        summarizer: Summarizer | None = None,
        keep_turns: int = 2,
    ) -> None:
        self.bus = bus
        self.llm = llm
        self.agent_id = agent_id
        self.store = store
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        # 工具表：name → Tool（schema + 实现 + 判量审批声明），测试可注入替身
        self.tools = tools if tools is not None else TOOLS
        # 手动压缩：摘要器（默认裸 chat 封顶档）+ 保留窗（最近 N 轮）
        self._summarizer = summarizer
        self.keep_turns = keep_turns
        self._pending_compact: dict[str, str] = {}
        self.trajectories: dict[str, Trajectory] = {}
        self.inboxes: dict[str, asyncio.Queue[Event]] = {}
        self._steering: dict[str, list[Event]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._inflight: dict[str, asyncio.Task[Any] | None] = {}
        self._turn_active: dict[str, bool] = {}
        self._phase: dict[str, str] = {}
        self._pending_interrupt: dict[str, dict[str, Any]] = {}
        # 人工确认：req_id → 正在等它的 future；sid → 当前那次等待的 req_id
        self._approvals: dict[str, asyncio.Future[Decision | None]] = {}
        self._approval_of_session: dict[str, str] = {}
        self.approval_timeout = approval_timeout
        # 当前 turn 的簇 id：本 turn 内 emit 的每个事件都带上它
        self._corr: dict[str, str] = {}
        bus.register(agent_id, self.enqueue)

    # -------------------------------------------------- 轨迹层：sid 的唯一入口

    def attach(self, traj: Trajectory) -> str:
        """登记一个会话（store.start/resume 的产物）。sid 从这里进 agent。

        本次运行的 system prompt 和轨迹记录（header / 已有 prompt_change）
        不一致——首次没记、或 resume 时换了模板——就追加一条 prompt_change
        留痕：prompt 变更是事实，不落盘审计就有洞。
        """
        self.trajectories[traj.sid] = traj
        recorded = str(traj.header.get("system_prompt", ""))
        for e in traj.path():
            if e.type == PROMPT_CHANGE and e.payload.get("system_prompt"):
                recorded = str(e.payload["system_prompt"])
        if recorded != self.system_prompt:
            traj.append(
                PROMPT_CHANGE,
                {"system_prompt": self.system_prompt, "by": "agent_attach"},
            )
        return traj.sid

    def _traj(self, sid: str) -> Trajectory:
        traj = self.trajectories.get(sid)
        if traj is None:
            raise KeyError(
                f"未知 session：{sid!r}——sid 由 SessionStore 分配（start/resume），"
                "再用 agent.attach(traj) 登记"
            )
        return traj

    def build_context(self, traj: Trajectory) -> Projection:
        """从轨迹现算上下文：system 与修复档位是 agent 自己的决定。

        build 是 agent 的活——轨迹层只管事实和树操作，怎么拼上下文、
        修不修、用什么 system，都由这里决定。每次现算，不缓存。
        """
        return build_context(traj)

    def messages(self, sid: str) -> list[dict[str, Any]]:
        """当前上下文（投影）：demo / 测试观测用，agent 自己在 step 前现算。"""
        return self.build_context(self._traj(sid)).messages

    # -------------------------------------------------- 手动压缩：第四种边界动作

    def _summarizer_for(self) -> Summarizer:
        """摘要器：外部注入优先（测试用 ScriptedSummarizer），默认裸 chat 封顶档。"""
        return self._summarizer if self._summarizer is not None else LiveSummarizer(self.llm)

    @staticmethod
    def _prefix_view(traj: Trajectory, keep_from_id: str) -> list[dict[str, Any]]:
        """被压段的视图消息：刀口之前的 entries，按 build_context 同一份分派规则
        （message 1:1、branch_summary 变 <summary>、状态节点不产生消息、元数据跳过）。

        前缀里没有 compaction（maybe_compact 拒绝第二刀），不走压缩分支。
        不做 sanitize：摘要会渲染成文本喂给摘要模型，不进 chat 消息序列，
        孤儿工具结果也读得懂。
        """
        entries = traj.path()
        ki = next((i for i, e in enumerate(entries) if e.id == keep_from_id), None)
        if ki is None:
            return []
        raw: list[dict[str, Any]] = []
        for e in entries[:ki]:
            if e.type == MESSAGE:
                msg = dict(e.payload.get("message", {}))
                if msg.get("role") == "system":
                    continue  # system 是参数不是事实
                if e.payload.get("synthetic") or e.payload.get("note"):
                    msg["synthetic"] = bool(e.payload.get("synthetic"))
                    msg["note"] = str(e.payload.get("note", ""))
                raw.append(msg)
            elif e.type == BRANCH_SUMMARY:
                raw.append(
                    {"role": "user", "content": f"<summary>{e.payload.get('summary', '')}</summary>"}
                )
        return raw

    async def _compact(self, sid: str, traj: Trajectory, *, reason: str = "manual") -> None:
        """手动压缩：step 边界处换一副更短的视图，下一次 build_context 自动生效。

        只作用于视图，不改事实层：追加一个 compaction entry（摘要 + 刀口），
        原文全在轨迹里。摘要调用失败 fail-open：留痕、不 append、不挡 turn，
        下一个边界可重试。已有压缩时拒绝第二刀（折叠语义归 05）。
        EventLog 记的是"什么时候、因为什么、压了多少"，与轨迹的 entry 各答各的。
        """
        before = len(self.build_context(traj).messages)
        try:
            entry = await maybe_compact(
                traj,
                self._summarizer_for(),
                keep_turns=self.keep_turns,
                reason=reason,
                prefix_view=self._prefix_view,
            )
        except Exception as exc:  # noqa: BLE001 - fail-open：压缩救不了自己时不挡 turn
            await self._emit(
                "context_compact_failed", sid, {"reason": reason, "error": str(exc)}
            )
            return
        if entry is None:
            await self._emit(
                "context_compact_failed",
                sid,
                {"reason": reason, "error": "无可压缩段（轮数不足）或已有压缩（折叠归 05）"},
            )
            return
        after = len(self.build_context(traj).messages)
        await self._emit(
            "context_compacted",
            sid,
            {
                "entry_id": entry.id,
                "keep_from_id": entry.payload.get("keep_from_id"),
                "reason": reason,
                "messages_before": before,
                "messages_after": after,
                "summary": str(entry.payload.get("summary", "")),
            },
        )

    # -------------------------------------------------- outbound：只有一条路

    async def _emit(self, type: str, sid: str, payload: dict[str, Any]) -> Any:
        """发一个事件：落盘、分道、治理都在总线里做，agent 只看返回值。"""
        return await self.bus.emit(
            Event(type, sid, payload, correlation_id=self._corr.get(sid, ""))
        )

    # -------------------------------------------------- inbound 侧：只投递

    def enqueue(self, event: Event) -> None:
        """总线 inbound 的投递函数（**同步**）：分派完立刻返回。

        下行本身照旧：普通消息进收件箱排队。**只有两条旁路**——中断（Stage 3）
        和人工确认的答复（stage04）——因为它们都是"只对某一次等待有效"的东西：
        排进队列就等于永远递不到（等在那一头的不是队列）。

        用户消息的意图（照搬 stage04 的 UserMessage）：
        - FOLLOWUP（默认）：进收件箱排队，等当前 turn 结束作为新 turn 处理；
        - STEERING：升级成插话，暂存到 ``_steering``，下一个 step 边界拼进当前
          上下文（本轮不断）。打断 / 审批答复是独立旁路，不在这里。
        """
        if event.type == "user_interrupt":
            self.on_interrupt(event)
            return
        if event.type == "user_approval":
            self.on_approval(event)
            return
        if event.type == "compact_request":
            self.on_compact(event)
            return
        if event.type == "user_input":
            sid = event.session_id
            if getattr(event, "intent", FOLLOWUP) == STEERING:
                self._steering.setdefault(sid, []).append(event)
            else:
                self.inboxes.setdefault(sid, asyncio.Queue()).put_nowait(event)
                if sid not in self._workers or self._workers[sid].done():
                    self._workers[sid] = asyncio.create_task(self._worker(sid))
            return
        sid = event.session_id
        self.inboxes.setdefault(sid, asyncio.Queue()).put_nowait(event)
        if sid not in self._workers or self._workers[sid].done():
            self._workers[sid] = asyncio.create_task(self._worker(sid))

    def promote(self, msg: UserMessage) -> None:
        """把一个 followup 升级成 steering（stage04 同名方法）。

        stage04 的收件箱是逐条取的队列，没有"取出某条 followup"的钩子；
        这里直接把消息追加进 steering 暂存——语义等价：它会在下一个 step
        边界拼进当前上下文。
        """
        sid = msg.session_id
        if sid not in self.inboxes and sid not in self._steering:
            return
        self._steering.setdefault(sid, []).append(msg)

    def on_interrupt(self, event: Event) -> None:
        """中断：控制信号，不进收件箱（**同步**，无 await）。

        payload 可带 text：用户在打断的同时输入的新消息。带 text → turn 不结束，
        消息在收尾时拼进上下文；不带 → 纯停止，turn 到占位/封口为止。
        落空的中断不留痕：什么都没发生，就没有事实可记。
        """
        sid = event.session_id
        task = self._inflight.get(sid)
        active = bool(self._turn_active.get(sid)) or task is not None
        if active:
            self._pending_interrupt[sid] = {"text": event.payload.get("text")}
            # 正在等人工确认：把这次等待直接结束（`None` = 既不批也不拒）
            self._settle_approval(sid, None)
            if task is not None and not task.done() and self._phase.get(sid) == "stream":
                task.cancel()

    # -------------------------------------------------- 人工确认：答复怎么进来

    def on_compact(self, event: Event) -> None:
        """手动压缩命令：控制信号，不进收件箱（**同步**，无 await）。

        只置标记，真正的压缩在下一个 step 边界做——流中间不换上下文，
        在飞请求的视图不漂移（与 steering 等边界、与 interrupt 同一条纪律）。
        agent 空闲时到的命令，下一个 turn 的第一个边界生效。
        落空（未知 session）不留痕：什么都没发生，就没有事实可记。
        """
        sid = event.session_id
        if sid in self.trajectories:
            self._pending_compact[sid] = str(event.payload.get("reason", "manual"))

    def on_approval(self, event: Event) -> None:
        """人工确认的答复（**同步**，无 await）：不进收件箱，直接交给正在等它的 future。

        没人等它（号不对 / 迟到 / 重复）也**必须留痕**（走总线的同步入口 record），
        但不伪造一次裁决。
        """
        req_id = str(event.payload.get("request_id", ""))
        fut = self._approvals.get(req_id)
        if fut is None or fut.done():
            self.bus.record(
                Event(
                    "approval_reply",
                    event.session_id,
                    {
                        "request_id": req_id,
                        "approve": bool(event.payload.get("approve")),
                        "reason": str(event.payload.get("reason", "")),
                        "stale": True,
                    },
                    correlation_id=self._corr.get(event.session_id, ""),
                )
            )
            return
        fut.set_result(self._verdict_from(event.payload))

    @staticmethod
    def _verdict_from(payload: dict[str, Any]) -> Decision:
        """一条答复 → 一个裁决：批 = ALLOW、拒 = DENY、批并改了参数 = MODIFY。"""
        reason = str(payload.get("reason", ""))
        if not payload.get("approve"):
            return Decision.deny("user", reason or "用户拒绝")
        arguments = payload.get("arguments")
        if arguments is not None:
            return Decision(
                action=MODIFY,
                by="user",
                reason=reason or "用户批准，并改写了参数",
                patch={"arguments": str(arguments)},
            )
        return Decision.allow("user", reason or "用户批准")

    def _settle_approval(self, sid: str, verdict: Decision | None) -> bool:
        """结束本 session 正在等的那次确认（`None` = 被中断，既不批也不拒）。"""
        req_id = self._approval_of_session.get(sid)
        if req_id is None:
            return False
        fut = self._approvals.get(req_id)
        if fut is None or fut.done():
            return False
        fut.set_result(verdict)
        return True

    async def stop(self) -> None:
        """显式收尾：取消在飞的 step 和所有 worker，并等它们退出。"""
        for task in self._inflight.values():
            if task is not None and not task.done():
                task.cancel()
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            try:
                await task
            except asyncio.CancelledError:
                pass

    # -------------------------------------------------- worker：消费侧

    async def _worker(self, sid: str) -> None:
        inbox = self.inboxes[sid]
        while True:
            event = await inbox.get()
            await self._run_turn(event)

    async def _drain_steering(self, sid: str, traj: Trajectory) -> int:
        """step 边界 drain：此刻 inbox 里的消息 + 暂存的 steering 全部当 steering。

        STEERING 意图的消息（或经 promote 升级的）直接拼进当前上下文、本轮不断；
        FOLLOWUP 意图的（默认）也在这里一并拼入——stage04 收敛了 stage04 的
        两条队列，step 边界一次 drain。
        """
        inbox = self.inboxes[sid]
        steered = self._steering.pop(sid, [])
        texts: list[str] = []
        while not inbox.empty():
            ev = inbox.get_nowait()
            texts.append(ev.payload["text"])
            traj.append(
                MESSAGE, message_payload({"role": "user", "content": ev.payload["text"]})
            )
        for ev in steered:
            texts.append(ev.payload["text"])
            traj.append(
                MESSAGE, message_payload({"role": "user", "content": ev.payload["text"]})
            )
        if texts:
            await self._emit("steering_consumed", sid, {"texts": texts})
        return len(texts)

    async def _step(
        self, sid: str, context: list[dict[str, Any]], partial: dict[str, Any]
    ) -> dict[str, Any]:
        """消费一轮流式输出：文本边到边发 agent_delta，边累积，流结束拼完整消息。

        partial 只是累积缓冲（调用方 _run_step 的字典）：step 正常返回时由
        调用方拼成完整消息；被取消时随 task 一起消失——没收到完整的 LLM
        返回就当没收到，没有半成品要抢救。
        """
        text_parts: list[str] = partial["text_parts"]
        tool_calls: dict[int, dict[str, str]] = partial["tool_calls"]
        tool_started = False
        async for chunk in self.llm.stream_chat(context):
            if self._pending_interrupt.get(sid) is not None:
                # 中断已在飞：立刻停手，不再发任何增量——stop 之后再冒出
                # thinking/文本残片会破坏呈现顺序。直接中止，收尾交给
                # _run_steps 的取消路径（与 task.cancel() 殊途同归）。
                raise _StepAborted()
            if chunk["type"] == "reasoning_delta":
                await self._emit("agent_thinking", sid, {"text": chunk["text"]})
            elif chunk["type"] == "text_delta":
                text_parts.append(chunk["text"])
                await self._emit("agent_delta", sid, {"text": chunk["text"]})
            elif chunk["type"] == "tool_call_delta":
                if not tool_started:
                    tool_started = True
                    await self._emit("tool_call_started", sid, {})
                tc = tool_calls.setdefault(
                    chunk["index"], {"id": "", "name": "", "args": ""}
                )
                if chunk.get("id"):
                    tc["id"] = chunk["id"]
                if chunk.get("name"):
                    tc["name"] = chunk["name"]
                tc["args"] += chunk.get("args_delta", "")
        if tool_calls:
            return {
                "role": "assistant",
                "content": "".join(text_parts) or None,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["args"]},
                    }
                    for _, tc in sorted(tool_calls.items())
                ],
            }
        return {"role": "assistant", "content": "".join(text_parts)}

    async def _run_step(
        self, sid: str, traj: Trajectory, partial: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """一个可中断的完整单元：投影出上下文 → 消费流 → 执行工具（若有）。

        上下文是**当场从轨迹投影出来的**（当场从轨迹算出），不是攒在内存里的
        list——rewind / 压缩之后，下一次调用自动就是新视图。
        """
        partial["text_parts"] = []
        partial["tool_calls"] = {}
        self._phase[sid] = "stream"
        context = self.build_context(traj).messages
        msg = await self._step(sid, context, partial)
        # assistant 消息一成形就先发总线——必须在工具执行之前：否则轨迹里会先
        # 出现 tool_result、后出现发起它的 tool_call。
        await self._emit("agent_reply", sid, {"message": msg})
        tool_results: list[dict[str, Any]] = []
        calls = msg.get("tool_calls") or []
        if calls:
            self._phase[sid] = "tools"
        for call in calls:
            name = call["function"]["name"]
            if self._pending_interrupt.get(sid) is not None:
                # 已经收到中断：这一批剩下的调用一个都别问、别跑
                result, blocked, skipped = NO_EXEC, False, True
            else:
                result, blocked, skipped = await self._execute_call(sid, call)
            await self._emit(
                "tool_result",
                sid,
                {
                    "tool_call_id": call["id"],
                    "name": name,
                    "result": result,
                    "skipped": skipped,
                    "blocked": blocked,
                },
            )
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                }
            )
        partial["tool_results"] = tool_results
        return msg, tool_results

    async def _execute_call(
        self, sid: str, call: dict[str, Any]
    ) -> tuple[str, bool, bool]:
        """跑一次工具调用，返回 `(结果文本, 是否被拦, 是否未执行)`。

        执行前的两道关卡都在工具执行路径上，顺序就是"先能判的、再要等的"：

        1. **治理**（before_tool_call 拦截器，当场）：DENY → 不执行；MODIFY →
           按改过的参数执行。
        2. **审批**（工具声明 `Tool.approval_check` 判量，要等人）：声明返回
           理由就发 `approval_required`，等答复。等不到（超时）按拒绝处理；
           等的时候被中断（答复是 `None`）→ 这个调用算没执行。
           "要不要问人"归工具自己，不归总线订阅者。
        3. 放行：参数的最终形态 = 规则改写（在 `gate.event.payload` 上）
           → 人的改写（在答复的 patch 上）→ 执行。
        """
        name = call["function"]["name"]
        args_text = call["function"]["arguments"]
        gate = await self._emit(
            "before_tool_call",
            sid,
            {"name": name, "call_id": call["id"], "arguments": args_text},
        )
        if not gate.allowed:
            reason = "; ".join(d.reason for d in gate.decisions if d.action == DENY)
            return f"{BLOCKED_PREFIX}：{reason or '被规则拒绝'}", True, False
        # 放行：以 emit 返回的事件为准（规则可能改写过参数）
        args_text = str(gate.event.payload.get("arguments", args_text))
        tool = self.tools.get(name)
        if tool is None:
            return f"未知工具：{name}", False, False

        # 审批：工具声明判量（本地理由优先），拦截器的 ASK（若有）兼容并存
        declared = ""
        if tool.approval_check is not None:
            try:
                declared = tool.approval_check(json.loads(args_text)) or ""
            except (TypeError, ValueError):
                declared = "参数无法解析，需人工确认"
        if gate.needs_approval or declared:
            reason = declared or "; ".join(
                d.reason for d in gate.decisions if d.action == ASK
            )
            verdict = await self._request_approval(sid, name, call["id"], args_text, reason)
            if verdict is None:
                return APPROVAL_ABANDONED, False, True  # 等待期间被中断：没批也没拒
            if verdict.action == DENY:
                prefix = (
                    APPROVAL_TIMEOUT
                    if verdict.by == APPROVAL_TIMEOUT_BY
                    else APPROVAL_REJECTED
                )
                return f"{prefix}：{verdict.reason or '未通过确认'}", True, False
            # 人的改写最后覆盖规则的改写
            if verdict.patch:
                args_text = str(verdict.patch.get("arguments", args_text))
        return await tool.fn(json.loads(args_text)), False, False

    async def _request_approval(
        self, sid: str, name: str, call_id: str, arguments: str, reason: str
    ) -> Decision | None:
        """发一条 `approval_required`，然后等答复。返回 `None` = 这次等待被中断掉了。

        三件事的顺序不能换：先登记 future 再发请求（答复可能比 await 先到）；
        等答复的是 agent 不是拦截器；超时按拒绝（fail-closed）。
        一问必有一答：不管结局是什么，都紧跟一条 approval_decided 回执。
        """
        req_id = f"ap-{uuid.uuid4().hex[:8]}"
        fut: asyncio.Future[Decision | None] = asyncio.get_running_loop().create_future()
        self._approvals[req_id] = fut
        self._approval_of_session[sid] = req_id
        self._phase[sid] = "awaiting_approval"
        verdict: Decision | None = None
        try:
            await self._emit(
                "approval_required",
                sid,
                {
                    "request_id": req_id,
                    "name": name,
                    "call_id": call_id,
                    "arguments": arguments,
                    "reason": reason,
                    "timeout": self.approval_timeout,
                },
            )
            try:
                verdict = await asyncio.wait_for(fut, self.approval_timeout)
            except asyncio.TimeoutError:
                verdict = Decision.deny(
                    APPROVAL_TIMEOUT_BY, f"等人工确认超过 {self.approval_timeout:g}s"
                )
        finally:
            self._approvals.pop(req_id, None)
            if self._approval_of_session.get(sid) == req_id:
                self._approval_of_session.pop(sid, None)
            self._phase[sid] = "tools"
        await self._emit(
            "approval_decided",
            sid,
            {
                "request_id": req_id,
                "action": verdict.action if verdict is not None else "abandoned",
                "by": verdict.by if verdict is not None else "user_interrupt",
                "reason": (
                    verdict.reason
                    if verdict is not None
                    else "等待期间被中断，未授权"
                ),
                "arguments": (verdict.patch or {}).get("arguments") if verdict else None,
            },
        )
        return verdict

    async def _run_turn(self, event: Event) -> None:
        sid = event.session_id
        traj = self._traj(sid)
        # 一次 turn 一个簇 id：本 turn 内 emit 的所有事件共享它
        self._corr[sid] = f"turn-{uuid.uuid4().hex[:8]}"
        traj.append(
            MESSAGE, message_payload({"role": "user", "content": event.payload["text"]})
        )
        await self._emit("user_input", sid, {"text": event.payload["text"]})
        self._turn_active[sid] = True
        try:
            await self._run_steps(sid, traj)
        finally:
            self._turn_active[sid] = False
            self._phase.pop(sid, None)
            self._pending_interrupt.pop(sid, None)
            self._pending_compact.pop(sid, None)

    async def _run_steps(self, sid: str, traj: Trajectory) -> None:
        """turn 主循环：step 边界查中断、查压缩、跑一步、结算。"""
        for _ in range(MAX_STEPS):
            pending = self._pending_interrupt.pop(sid, None)
            if pending is not None:
                if pending.get("text"):
                    # 边界上带着新消息：没有残破消息可修，就是一条普通 user 消息
                    # （语义上等于 steering），turn 继续。
                    await self._append_synth(
                        sid,
                        traj,
                        {"role": "user", "content": str(pending["text"])},
                        "redirect",
                    )
                    await self._mark_boundary(sid)
                    continue
                await self._close_stop(sid, traj)
                await self._mark_boundary(sid)
                await self._end_turn(sid, "interrupted")
                return
            compact_reason = self._pending_compact.pop(sid, None)
            if compact_reason is not None:
                # 第四种边界动作：换一副更短的视图，再发下一次调用。
                # 不消耗 step 预算；摘要失败 fail-open，不挡 turn。
                await self._compact(sid, traj, reason=compact_reason)
            await self._drain_steering(sid, traj)
            partial: dict[str, Any] = {}
            step_task = asyncio.create_task(self._run_step(sid, traj, partial))
            self._inflight[sid] = step_task
            try:
                msg, tool_results = await step_task
            except _StepAborted:
                # 流里主动中止（pending_interrupt 已置位，不等 cancel 时序）：
                # 与下面 task.cancel() 那条路收尾完全一致。
                if await self._settle_aborted(sid, traj):
                    continue
                return
            except asyncio.CancelledError:
                # 区分两种取消：step_task 被 on_interrupt 掐掉（本 turn 交给
                # 我们收尾），或者 turn 协程自己被 stop() 掐掉（继续往外抛）。
                if not step_task.cancelled():
                    raise
                if await self._settle_aborted(sid, traj):
                    continue
                return
            finally:
                if self._inflight.get(sid) is step_task:
                    self._inflight[sid] = None
            # append 只发生在 step 成功结算之后：被掐的 step 不留半截消息
            traj.append(MESSAGE, message_payload(msg))
            if not tool_results:
                await self._end_turn(sid, "turn end")
                return
            for tr in tool_results:
                traj.append(MESSAGE, message_payload(tr))
        await self._end_turn(sid, "max steps")

    async def _settle_aborted(
        self, sid: str, traj: Trajectory
    ) -> bool:
        """在飞 step 被取消/中止后的统一收尾：发 step_cancelled（总线事件，UI 用），
        再决定 turn 走向。"""
        self._inflight[sid] = None
        pending = self._pending_interrupt.pop(sid, None) or {"text": None}
        await self._emit("step_cancelled", sid, {})
        return await self._close_after_cancel(sid, traj, pending)

    async def _close_after_cancel(
        self,
        sid: str,
        traj: Trajectory,
        pending: dict[str, Any],
    ) -> bool:
        """流式输出被掐之后的收尾。返回 True 表示同一个 turn 还要继续（附了新消息）。

        没收到完整的 LLM 返回就当没收到：在飞 step 的产物一律丢，收尾只补
        合成的占位与新消息，不推断模型意图。
        """
        text = pending.get("text")
        if text:
            # 收口补一条 assistant 打断占位（和纯停止同一条规则）；新消息保持
            # 纯用户文本，不带任何标注。
            await self._append_synth(
                sid,
                traj,
                {"role": "assistant", "content": INTERRUPTED},
                "interrupted",
            )
            await self._append_synth(
                sid,
                traj,
                {"role": "user", "content": str(text)},
                "redirect",
            )
            return True
        await self._close_stop(sid, traj)
        await self._end_turn(sid, "interrupted")
        return False

    async def _close_stop(self, sid: str, traj: Trajectory) -> None:
        """stop 的收口形状，只在这一处决定——边界命中和掐在跑两条路共用。

        一律补 assistant 打断占位，文本按尾部角色选：尾部是 `tool`（工具结果
        没人接）用"不再作答"文本；尾部是 `user`（没被回答的问题）用被打断声明——
        不补的话下一轮模型会把它翻出来重答。
        """
        if traj.last_message_role() == "tool":
            await self._append_synth(
                sid, traj, {"role": "assistant", "content": STOP_CLOSER}, "marker"
            )
        else:
            await self._append_synth(
                sid, traj, {"role": "assistant", "content": INTERRUPTED}, "marker"
            )

    async def _mark_boundary(self, sid: str) -> None:
        await self._emit("turn_interrupted", sid, {})

    async def _append_synth(
        self,
        sid: str,
        traj: Trajectory,
        message: dict[str, Any],
        note: str,
        tool_name: str = "",
    ) -> None:
        """把一条**合成**消息追加进轨迹，并作为一条事件 emit 出去。

        合成消息（打断占位、封口占位、新消息）也必须进事实层，
        否则"history 是 log 的投影"就在这里断了。payload 里带 synthetic 和 note
        标明它不是真发生过的对话（投影时原样带出，sanitize 时才 strip）。
        """
        traj.append(MESSAGE, message_payload(message, synthetic=True, note=note))
        role = message.get("role")
        if role == "assistant":
            await self._emit(
                "agent_reply",
                sid,
                {"message": message, "synthetic": True, "note": note},
            )
        elif role == "tool":
            await self._emit(
                "tool_result",
                sid,
                {
                    "tool_call_id": message.get("tool_call_id", ""),
                    "name": tool_name,
                    "result": message.get("content", ""),
                    "skipped": True,
                    "synthetic": True,
                    "note": note,
                },
            )
        else:
            await self._emit(
                "user_input",
                sid,
                {
                    "text": message.get("content", ""),
                    "synthetic": True,
                    "note": note,
                },
            )

    async def _end_turn(self, sid: str, reason: str) -> None:
        await self._emit("turn_end", sid, {"reason": reason})
