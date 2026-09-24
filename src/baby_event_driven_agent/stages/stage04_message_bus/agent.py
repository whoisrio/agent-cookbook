"""agent：stage03 的全部能力（收件箱 / steering / 打断 / 转向），outbound 改走新总线。

本章 agent 侧的改动只有三处，都很小——这正是把机制放在总线上的意义：

1. 所有 outbound 走 `self._emit(...)`：分道、治理都由总线在 emit 里做。
2. 工具执行前两道关卡都在执行路径上：治理（governor 规则链，当场判
   deny/modify）和审批（工具声明的 `requires_approval`，要等人）。
3. **要等人的那一半**：工具声明要审批（`Tool.requires_approval`，声明在
   `tools.py` 的工具定义上）时，agent 发一条 `approval_required` 出去，然后
   自己 await 一个 future；答复由 inbound 的 `user_approval` 直接 resolve
   （**不进收件箱**——等在那一头的不是队列）。等不到就按拒绝处理
   （fail-closed），答复无论批/拒/超时都发成 `approval_decided` 事件：
   谁、什么时候、因为什么批的，都由事件带着走。

中断与转向的逻辑一字未动：它们是 Stage 3 的事，跟“事件怎么到达消费者”无关。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

from .bus import EventBus
from .events import ALLOW, DENY, MODIFY, STEERING, Decision, Event, UserMessage
from .governance import Governor
from .tools import TOOLS, Tool, build_system_prompt

logger = logging.getLogger(__name__)

MAX_STEPS = 4

# 中断收尾用的标记与占位（同 stage03，自描述是硬要求）
STOP_MARKER = "[本轮已被用户中断，不要回答上面那条问题]"
STOP_CLOSER = "[本轮已被用户中断，不再基于上面的工具结果作答]"
REDIRECT_NOTE = "[上一轮回答被用户打断，以下是用户的纠正]"
NO_EXEC = "[被用户中断，未执行]"
INTERRUPTED_SHELL = "[response interrupted]"
# 治理否决的占位：同样要自描述，模型得知道这不是工具的输出
BLOCKED_PREFIX = "[被规则拦截，未执行]"
# 人工确认的结局，各自一个自描述占位：拒绝、超时（fail-closed）、被中断放弃
# 用"未授权"而不是"未执行"：这三条的共同点是**没拿到授权**，跟"规则拦下"不是一回事
APPROVAL_REJECTED = "[人工确认未通过，未授权执行]"
APPROVAL_TIMEOUT = "[人工确认超时，未授权执行]"
APPROVAL_ABANDONED = "[等待人工确认期间被中断，未授权执行]"
# 超时那条裁决的署名：占位和 log 里都靠它把"人拒了"和"没人答"分开
APPROVAL_TIMEOUT_BY = "approval_timeout"

SYSTEM_PROMPT = build_system_prompt()


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
        approval_timeout: float = 30.0,
        tools: dict[str, Tool] | None = None,
        governor: Governor | None = None,
    ) -> None:
        self.bus = bus
        self.llm = llm
        self.agent_id = agent_id
        self.tools = tools if tools is not None else TOOLS
        self.governor = governor if governor is not None else Governor()
        self.history: dict[str, list[dict[str, Any]]] = {}
        # 收件箱按用户意图分两条**按序列表**（append 到达，消费时取
        # (priority, 到达序号) 最小值——同优先级 FIFO 天然成立）。
        # 不用 PriorityQueue：它只支持按堆序弹出，无法精确移动"用户指定的
        # 那一条"，promote（followup → steering 的升级）会带出顺序问题。
        # 列表 + 单线程事件循环内的同步操作（remove/append 之间无 await），
        # 让 promote 与 worker 的取件天然互斥。
        # followup：默认去向，只等 worker 空闲（当前 turn 结束后）才被消费；
        # steering：turn 在飞时在 step 边界被 drain 进当前上下文；空闲时被
        # worker 取到则自然降级成主输入（无可插对象）。
        self._followups: dict[str, list[tuple[int, int, Event]]] = {}
        self._steerings: dict[str, list[tuple[int, int, Event]]] = {}
        self._wakes: dict[str, asyncio.Event] = {}
        self._arrivals = itertools.count()
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._inflight: dict[str, asyncio.Task[Any] | None] = {}
        self._turn_active: dict[str, bool] = {}
        self._phase: dict[str, str] = {}
        self._pending_interrupt: dict[str, dict[str, Any]] = {}
        # 人工确认：req_id → 正在等它的 future；sid → 当前那次等待的 req_id
        self._approvals: dict[str, asyncio.Future[Decision | None]] = {}
        self._approval_of_session: dict[str, str] = {}
        self.approval_timeout = approval_timeout
        bus.register(agent_id, self.enqueue)

    # -------------------------------------------------- outbound：只有一条路

    async def _emit(self, type: str, sid: str, payload: dict[str, Any]) -> None:
        """发一个事件：分派订阅者，总线就做这一件事。"""
        await self.bus.emit(Event(type, sid, payload))

    # -------------------------------------------------- inbound 侧：只投递

    def enqueue(self, event: Event) -> None:
        """总线 inbound 的投递函数（**同步**）：分派完立刻返回。

        下行消息按用户意图分派到两条收件箱，**控制权在发布端**：

        ```
        followup inbox（默认）：排队，只等 worker 空闲（当前 turn 结束后）消费
        steering inbox：       turn 在飞时，step 边界 drain 进当前上下文；
                               空闲时被 worker 取到 → 自然降级成主输入
        旁路（同步直达，先于一切队列）：user_interrupt → on_interrupt
                                       user_approval  → on_approval
        ```

        两条队列内部都按 `event.priority` 出队（小的先），同优先级按到达先后。
        排队的消息必须是 `UserMessage`——路由元数据（intent / priority）声明在
        那个类型上；旁路事件（打断 / 审批答复）不排队，不经过这里。
        Stage 2 的"分类权在消费端"到本章被演进取代：steering 不再是消费时机
        的推论，而是用户显式指定的意图；消费端只负责执行意图与降级。
        """
        if event.type == "user_interrupt":
            self.on_interrupt(event)
            return
        if event.type == "user_approval":
            self.on_approval(event)
            return
        if not isinstance(event, UserMessage):
            raise TypeError(
                f"收件箱只收用户消息（UserMessage），收到 {type(event).__name__}"
                f"（{event.type}）：排队元数据声明在 UserMessage 上"
            )
        sid = event.session_id
        item = (event.priority, next(self._arrivals), event)
        if event.intent == STEERING:
            self._steerings.setdefault(sid, []).append(item)
        else:
            self._followups.setdefault(sid, []).append(item)
        self._wakes.setdefault(sid, asyncio.Event()).set()
        if sid not in self._workers or self._workers[sid].done():
            self._workers[sid] = asyncio.create_task(self._worker(sid))

    def promote(self, event: Event) -> bool:
        """把一条还在 followup 队列里的消息升级为 steering（用户点名插话）。

        精确移动那一条（remove 定位，不碰别的消息）。返回是否成功：
        消息已被 worker 取走（正在处理或已处理）时不在队列里，返回 False——
        此时它无法再插话。同步操作（无 await），与 worker 的取件天然互斥，
        不存在"turn 刚结束的取件窗口"竞态。
        """
        sid = event.session_id
        queue = self._followups.get(sid, [])
        item = next((it for it in queue if it[2] is event), None)
        if item is None:
            return False
        queue.remove(item)
        self._steerings.setdefault(sid, []).append(item)
        return True

    def on_interrupt(self, event: Event) -> None:
        """中断：控制信号，不进收件箱（**同步**，无 await）。

        落空的中断不留痕：什么都没发生，就没有事实可记。生效的中断会变成
        step_cancelled / turn_interrupted（都带 intent 落盘），不需要再记一笔“收到”。
        """
        sid = event.session_id
        intent = str(event.payload.get("intent", "stop"))
        task = self._inflight.get(sid)
        active = bool(self._turn_active.get(sid)) or task is not None
        if active:
            self._pending_interrupt[sid] = {
                "intent": intent,
                "text": event.payload.get("text"),
            }
            # 正在等人工确认：把这次等待直接结束（`None` = 既不批也不拒），
            # 让 step 跑完、在 step 边界按中断结算——和 stage03“工具执行中不掐、
            # 跑完在边界收尾”是同一个形状。不这么做的话，用户按停止后要干等到
            # 确认超时，turn 才动得了。
            self._settle_approval(sid, None)
            if task is not None and not task.done() and self._phase.get(sid) == "stream":
                task.cancel()

    # -------------------------------------------------- 人工确认：答复怎么进来

    def on_approval(self, event: Event) -> None:
        """人工确认的答复（**同步**，无 await）：不进收件箱，直接交给正在等它的 future。

        没人等它（号不对 / 迟到 / 重复）就不认领——本章不伪造一次裁决：
        迟到的输入没有改变任何对话状态，不需要留档。
        """
        req_id = str(event.payload.get("request_id", ""))
        fut = self._approvals.get(req_id)
        if fut is None or fut.done():
            logger.info(
                "答复 %s 没有人在等（迟到 / 重复 / 号不对），不认领",
                req_id,
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
        while True:
            event = await self._next_message(sid)
            await self._run_turn(event)

    def _pop_next(self, sid: str) -> Event | None:
        """取下一条：**类序固定**——steering inbox 先于 followup inbox。

        promote / 点名插话是用户在说"这条别等了"，它的先后不该再和 followup
        拿 priority 数字比（否则一个 priority=10 的 followup 就能压过显式
        promote 的插话）。priority 只在同类内部排序（小的先，同级按到达序）。
        turn 在飞时取到的 steering 由 step 边界 drain（插话生效）；worker
        空闲时取到的 steering 没有可插对象，自然降级成主输入——同一条
        取件规则，两种表现。
        """
        st = self._steerings.get(sid)
        if st:
            item = min(st)
            st.remove(item)
            return item[2]
        fu = self._followups.get(sid)
        if fu:
            item = min(fu)
            fu.remove(item)
            return item[2]
        return None

    async def _next_message(self, sid: str) -> Event:
        """等待并取下一条消息。空队列时挂在唤醒事件上（enqueue 会 set）。"""
        while True:
            event = self._pop_next(sid)
            if event is not None:
                return event
            wake = self._wakes.setdefault(sid, asyncio.Event())
            wake.clear()
            # 双检查防丢唤醒：clear 与 enqueue 的 set 竞争（enqueue 先 append 后 set）
            if self._followups.get(sid) or self._steerings.get(sid):
                continue
            await wake.wait()

    async def _drain_steering(self, sid: str, history: list[dict[str, Any]]) -> int:
        """step 边界 drain：steering inbox 里用户点名插话的消息，按优先级拼进上下文。

        只消费 steering 队列——followup 是用户明确说"等下一轮"的，不在这里碰。
        """
        inbox = self._steerings.get(sid)
        if not inbox:
            return 0
        texts: list[str] = []
        for _, _, ev in sorted(inbox):  # (priority, 到达序) 序
            texts.append(ev.payload["text"])
            history.append({"role": "user", "content": ev.payload["text"]})
        inbox.clear()
        if texts:
            await self._emit("steering_consumed", sid, {"texts": texts})
        return len(texts)

    async def _step(
        self, sid: str, history: list[dict[str, Any]], partial: dict[str, Any]
    ) -> dict[str, Any]:
        """消费一轮流式输出：文本边到边发 agent_delta，边累积，流结束拼完整消息。

        累积结果写进 partial 而不是局部变量——取消会把局部变量一起带走，
        而“掐完之后要不要保留半成品”得看得到它们。
        """
        text_parts: list[str] = partial["text_parts"]
        tool_calls: dict[int, dict[str, str]] = partial["tool_calls"]
        tool_started = False
        async for chunk in self.llm.stream_chat(history):
            if chunk["type"] == "reasoning_delta":
                partial["thinking_seen"] = True
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
        self, sid: str, history: list[dict[str, Any]], partial: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """一个可中断的完整单元：消费流 + 执行工具（若有）。

        工具执行前先过治理（governor 规则链）：被否决就不执行（补一条自描述
        占位），被改写就执行改过的参数。裁决的事实由占位带回上下文，事后答得
        出“这个工具为什么没执行”。
        """
        partial["text_parts"] = []
        partial["tool_calls"] = {}
        partial["thinking_seen"] = False
        self._phase[sid] = "stream"
        msg = await self._step(sid, history, partial)
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

        执行前的两道关卡都在这里（工具执行路径上，不在总线上），顺序就是
        "先能判的、再要等的"：

        1. **治理**（governor 规则链，当场）：DENY → 不执行；MODIFY →
           按改过的参数执行。
        2. **审批**（工具声明 `Tool.requires_approval`，要等人）：发
           `approval_required`，等答复。等不到（超时）按拒绝处理；等的
           时候被中断（答复是 `None`）→ 这个调用算没执行。
        3. 放行：参数的最终形态 = 规则改写 → 人的改写 → 执行。
        """
        name = call["function"]["name"]
        args_text = call["function"]["arguments"]
        verdict = await self.governor.check(name, args_text)
        if not verdict.allowed:
            reason = "; ".join(d.reason for d in verdict.decisions if d.action == DENY)
            return f"{BLOCKED_PREFIX}：{reason or '被规则拒绝'}", True, False
        args_text = verdict.arguments

        approved_by_human = False
        answer: Decision | None = None
        tool = self.tools.get(name)
        if tool is not None and tool.requires_approval:
            answer = await self._request_approval(
                sid,
                name,
                call["id"],
                args_text,
                tool.approval_reason or f"工具 {name} 需要人工确认",
            )
            if answer is None:
                return APPROVAL_ABANDONED, False, True  # 等待期间被中断：没批也没拒
            if answer.action == DENY:
                # "人拒了"和"没人答"要分得清：模型看到的占位、log 里的署名都得对得上
                prefix = (
                    APPROVAL_TIMEOUT
                    if answer.by == APPROVAL_TIMEOUT_BY
                    else APPROVAL_REJECTED
                )
                return f"{prefix}：{answer.reason or '未通过确认'}", True, False
            approved_by_human = True

        # 放行：人的改写最后覆盖规则的改写
        if approved_by_human and answer is not None and answer.patch:
            args_text = str(answer.patch.get("arguments", args_text))
        if tool is None:
            return f"未知工具：{name}", False, False
        return await tool.fn(json.loads(args_text)), False, False

    async def _request_approval(
        self, sid: str, name: str, call_id: str, arguments: str, reason: str
    ) -> Decision | None:
        """发一条 `approval_required`，然后等答复。返回 `None` = 这次等待被中断掉了。

        三件事的顺序不能换：

        - **先登记 future，再发请求**：答复可能比 `await` 先到（人的手速 + 订阅者的
          调度），先发后登记就会丢答复。
        - **等答复的是 agent，不是总线**：要不要问人由工具声明回答（当场），
          等人的时间以秒计，塞不进 emit 的毫秒预算。
        - **超时按拒绝**（fail-closed）：等的这段时间里，安全默认值只能是"没批就不干"。

        还有一条硬规则：**一问必有一答**。`approval_required` 发出去之后，不管结局是
        批准、拒绝、超时、还是被中断放弃，都必须紧跟一条 `approval_decided`——只有请求
        没有回执，看到事件的人就不知道这次确认到底结束了没有。唯一例外是进程被硬杀：
        那时和账本的"残尾"是一个道理，没写完的不算已发生（账本在下一章）。
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
        # 回执：一问必有一答。被中断放弃也算一种结局（action=abandoned），
        # 不能只发一条没人应答的 approval_required 就完事。
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
        history = self.history.setdefault(sid, [])
        if not history:
            history.append({"role": "system", "content": SYSTEM_PROMPT})
        history.append({"role": "user", "content": event.payload["text"]})
        await self._emit("user_input", sid, {"text": event.payload["text"]})
        self._turn_active[sid] = True
        try:
            await self._run_steps(sid, history)
        finally:
            self._turn_active[sid] = False
            self._phase.pop(sid, None)
            self._pending_interrupt.pop(sid, None)

    async def _run_steps(self, sid: str, history: list[dict[str, Any]]) -> None:
        """turn 主循环：step 边界查中断、跑一步、结算。"""
        for _ in range(MAX_STEPS):
            pending = self._pending_interrupt.pop(sid, None)
            if pending is not None:
                if pending.get("intent") == "redirect" and pending.get("text"):
                    await self._append_synth(
                        sid,
                        history,
                        {"role": "user", "content": str(pending["text"])},
                        "redirect",
                    )
                    await self._mark_boundary(sid, "redirect")
                    continue
                if history and history[-1].get("role") == "tool":
                    await self._append_synth(
                        sid,
                        history,
                        {"role": "assistant", "content": STOP_CLOSER},
                        "marker",
                    )
                else:
                    await self._append_synth(
                        sid,
                        history,
                        {"role": "user", "content": STOP_MARKER},
                        "marker",
                    )
                await self._mark_boundary(sid, "stop")
                await self._end_turn(sid, "interrupted")
                return
            await self._drain_steering(sid, history)
            partial: dict[str, Any] = {}
            step_task = asyncio.create_task(self._run_step(sid, history, partial))
            self._inflight[sid] = step_task
            try:
                msg, tool_results = await step_task
            except asyncio.CancelledError:
                if not step_task.cancelled():
                    raise
                self._inflight[sid] = None
                pending = self._pending_interrupt.pop(sid, None) or {
                    "intent": "stop",
                    "text": None,
                }
                await self._emit("step_cancelled", sid, {"intent": pending["intent"]})
                if await self._close_after_cancel(sid, history, pending, partial):
                    continue
                return
            finally:
                if self._inflight.get(sid) is step_task:
                    self._inflight[sid] = None
            history.append(msg)
            if not tool_results:
                await self._end_turn(sid, "turn end")
                return
            history.extend(tool_results)
        await self._end_turn(sid, "max steps")

    async def _close_after_cancel(
        self,
        sid: str,
        history: list[dict[str, Any]],
        pending: dict[str, Any],
        partial: dict[str, Any],
    ) -> bool:
        """流式输出被掐之后的收尾。返回 True 表示同一个 turn 还要继续（redirect）。"""
        text = pending.get("text")
        if pending.get("intent") == "redirect" and text:
            calls = partial.get("tool_calls") or {}
            parts = partial.get("text_parts") or []
            if calls:
                await self._append_synth(
                    sid, history, self._partial_assistant(partial), "interrupted"
                )
                for call in history[-1]["tool_calls"]:
                    await self._append_synth(
                        sid,
                        history,
                        {"role": "tool", "tool_call_id": call["id"], "content": NO_EXEC},
                        "tool skipped",
                        tool_name=call["function"]["name"],
                    )
            elif parts:
                await self._append_synth(
                    sid,
                    history,
                    {"role": "assistant", "content": "".join(parts)},
                    "interrupted",
                )
            elif partial.get("thinking_seen"):
                await self._append_synth(
                    sid,
                    history,
                    {"role": "assistant", "content": INTERRUPTED_SHELL},
                    "interrupted",
                )
            await self._append_synth(
                sid,
                history,
                {"role": "user", "content": f"{REDIRECT_NOTE}\n\n{text}"},
                "redirect",
            )
            return True
        await self._append_synth(
            sid, history, {"role": "user", "content": STOP_MARKER}, "marker"
        )
        await self._end_turn(sid, "interrupted")
        return False

    @staticmethod
    def _partial_assistant(partial: dict[str, Any]) -> dict[str, Any]:
        """把取消那一刻累积到的 tool_calls 拼成一条 assistant 消息。"""
        calls: dict[int, dict[str, str]] = partial.get("tool_calls") or {}
        text = "".join(partial.get("text_parts") or [])
        return {
            "role": "assistant",
            "content": text or None,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["args"]},
                }
                for _, tc in sorted(calls.items())
            ],
        }

    async def _mark_boundary(self, sid: str, intent: str) -> None:
        await self._emit("turn_interrupted", sid, {"intent": intent})

    async def _append_synth(
        self,
        sid: str,
        history: list[dict[str, Any]],
        message: dict[str, Any],
        note: str,
        tool_name: str = "",
    ) -> None:
        """把一条**合成**消息写进 history，并作为一条事件 emit 出去。

        合成消息（中断标记、assistant 占位、纠正 user、半成品）也必须进事实层，
        否则“history 是 log 的投影”就在这里断了。类型沿用投影认得的那三种
        （user_input / agent_reply / tool_result），靠 payload 里的 synthetic
        和 note 标明它不是真发生过的对话。
        """
        history.append(message)
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
