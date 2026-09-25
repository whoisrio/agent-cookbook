"""agent：收件箱 + 常驻 worker，steering 在 step 边界生效；Stage 3 加上可中断的在飞步骤。

总线从 Stage 2 起分了方向：inbound 的 publish 是同步的，按 agent_id 把命令
交给 `enqueue`；outbound 的事件改走 `emit` 扇出。

`enqueue` 只做一件事——按类型把命令分派下去就返回：user_input 投进对应 session
的收件箱，user_interrupt 交给 `on_interrupt`。turn 不再占着总线回调。每个
session 一个收件箱加一个常驻 worker task（第一次收到该 session 的消息时启动），
worker 循环"取消息 → 跑 turn"，turn 结束回到取消息——排在 turn 之后的消息
（followup）就是下一次 get 到的东西。

消息语义是消费那一刻定的，不是提交时定的：
- worker 空闲时取到 → 新 turn 的输入（followup）
- turn 在跑、step 边界 drain 到 → 拼进当前上下文继续走（steering）
- 同一条消息，落在哪个窗口就是什么，自己不背语义

worker 永不自行退出（只响应 stop 的 cancel），所以"消息进队之后
task 恰好死掉"的竞态在这个设计里根本不存在——退出清理是 stop 的
显式职责，不是每个 turn 的尾部负担。

Stage 3 只在这个地基上加中断，总线一行没动：

- user_interrupt 和 user_input 一样从 inbound 进来，但它不是消息：不进收件箱、
  不参与 steering / followup 的分类，直接作用在"正在飞的那一步"上。
- 每 session 记着当前在飞的 step task（`_inflight`）。`on_interrupt` 收到信号
  时检查并 cancel 它——整段没有 await，asyncio 单线程事件循环里是原子的，
  cancel 作用在具体的 task 对象上，不在飞的 session 查不到 task，信号自然落空，
  不会残留下来把下一个 turn 掐死。
- 取消的粒度是单步，不是 worker：step 被掐掉后由 turn 协程结算收尾，worker
  回到收件箱接着取消息，history 里已完成的步骤全部保留。
- stop()（进程收尾）与中断是两回事：stop 连在飞的 step 和 worker 一起收，
  中断只动当前 step，agent 活着。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from .events import Event, EventBus, SessionLog
from .llm import TOOLS, build_system_prompt

logger = logging.getLogger(__name__)

MAX_STEPS = 4

# 中断收尾用的标记与占位，都要自描述——不写清身份，模型会把它们当成
# 模型输出或工具输出（Hermes 早期用裸文本被当 prompt injection 拒过）。
STOP_MARKER = "[本轮已被用户中断，不要回答上面那条问题]"
STOP_CLOSER = "[本轮已被用户中断，不再基于上面的工具结果作答]"
REDIRECT_NOTE = "[上一轮回答被用户打断，以下是用户的纠正]"
NO_EXEC = "[被用户中断，未执行]"
# 只观测到 thinking、一个字可见文本都没有时，用空壳占住 assistant 的位置：
# 内容是声明"这里被打断过"，绝不回灌思维链本身。
INTERRUPTED_SHELL = "[response interrupted]"


class _StepAborted(Exception):
    """流里检测到待消费中断，主动中止本轮流式消费（不等 cancel 时序生效）。

    on_interrupt 同步置位 _pending_interrupt 后才调 task.cancel()；协作式 cancel
    真正生效前，模型流里可能还残几个增量被渲染出来、落到 stop 行之后。这里在
    _step 循环顶部直接 raise，保证 stop 之后绝不再发任何 LLM 增量。"""

# 完全没有 system prompt 时，实测模型会把"保温杯还有库存吗"当闲聊，
# 回一句"我无法访问实时库存"——得告诉它工具能摸到什么。工具的分工
# 不在这里手写，从 schemas 生成（路由信息只写在 description 一处）。
SYSTEM_PROMPT = build_system_prompt()


class LLMClient(Protocol):
    def stream_chat(
        self, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]: ...


class Agent:
    def __init__(
        self,
        bus: EventBus,
        log: SessionLog,
        llm: LLMClient,
        agent_id: str = "agent",
    ) -> None:
        self.bus = bus
        self.log = log
        self.llm = llm
        self.agent_id = agent_id
        self.history: dict[str, list[dict[str, Any]]] = {}  # session_id -> messages
        self.inboxes: dict[str, asyncio.Queue[Event]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        # Stage 3 的四样状态，都按 session 存、turn 结束即清：
        # _inflight：当前在飞的 step task，中断的靶子，空闲时是 None
        # _turn_active：区分"turn 内"和"空闲"（标志不能残留下一个 turn）
        # _phase：当前 step 走到哪一段（stream / tools），决定中断掐不掐
        # _pending_interrupt：已收到、还没被消费的中断，由 step 边界或取消分支消费
        self._inflight: dict[str, asyncio.Task[Any] | None] = {}
        self._turn_active: dict[str, bool] = {}
        self._phase: dict[str, str] = {}
        self._running_tool: dict[str, bool] = {}  # 工具是否正在执行（决定中断掐不掐）
        self._pending_interrupt: dict[str, dict[str, Any]] = {}
        # inbound 路由：publish(event, to=agent_id) 会落到 enqueue
        bus.register(agent_id, self.enqueue)

    # -------------------------------------------------- inbound 侧：只投递

    def enqueue(self, event: Event) -> None:
        """总线 inbound 的投递函数（**同步**）：分派完立刻返回。

        同步、且方法体内没有任何 await，整段就是原子的——put_nowait 和
        spawn 检查之间不可能被插入，"消息进了队但 worker 还没起"的窗口
        不存在。调用方也 await 不到它，更 create_task 不了它。

        按类型分派：消息进收件箱排队，中断是控制信号，直接作用在在飞的
        step 上。两者共用同一个投递入口（同一个收件地址 = 同一个 agent），
        区别只在进 agent 之后怎么走。
        """
        if event.type == "user_interrupt":
            self.on_interrupt(event)
            return
        sid = event.session_id
        self.inboxes.setdefault(sid, asyncio.Queue()).put_nowait(event)
        if sid not in self._workers or self._workers[sid].done():
            self._workers[sid] = asyncio.create_task(self._worker(sid))

    def on_interrupt(self, event: Event) -> None:
        """中断：控制信号，不进收件箱、不当消息分类（**同步**，无 await）。

        payload 带 intent：stop（掐掉、turn 结束）或 redirect（掐掉、原地转向），
        缺省 stop；redirect 还要带 text（纠正内容）。

        这里只做两件事：记下"待消费的中断"，以及在**流式输出这一段**把在飞的
        step 掐掉。工具执行那一段（_phase == "tools"）不掐——Stage 3 的工具默认
        不可中断，让它跑完、由 step 边界收尾。

        空闲时（没有 turn）什么都不置：标志残留在 session 上会把下一个 turn 掐死。
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
            # 流式输出这一段、或工具正在执行中：当场掐掉在飞的 step（真打断）。
            # 工具已返回、step 还在收尾（tool_done 落点）那一小段不掐——让 step
            # 跑完、由 step 边界收尾，工具结果才留得住（否则会随 cancel 一起丢）。
            if task is not None and not task.done() and (
                self._phase.get(sid) == "stream" or self._running_tool.get(sid)
            ):
                task.cancel()
        self.log.append(event, note="interrupt received")

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
        """常驻消费循环：取一条消息跑一个 turn，跑完接着取。"""
        inbox = self.inboxes[sid]
        while True:
            event = await inbox.get()
            await self._run_turn(event)

    async def _drain_steering(self, sid: str, history: list[dict[str, Any]]) -> int:
        """step 边界 drain：此刻 inbox 里的消息全部当 steering，拼进当前上下文。

        消费发生时发 steering_consumed 事件——demo 和 UI 靠它看见
        "插话在这一刻生效了"，而不是靠翻 session log。
        """
        inbox = self.inboxes[sid]
        texts: list[str] = []
        while not inbox.empty():
            ev = inbox.get_nowait()
            texts.append(ev.payload["text"])
            history.append({"role": "user", "content": ev.payload["text"]})
            self.log.append(ev, note="steering")
        if texts:
            await self.bus.emit(Event("steering_consumed", sid, {"texts": texts}))
        return len(texts)

    async def _step(
        self, sid: str, history: list[dict[str, Any]], partial: dict[str, Any]
    ) -> dict[str, Any]:
        """消费一轮流式输出：文本边到边发 agent_delta，边累积，流结束拼完整消息。

        累积结果写进 partial 而不是局部变量——取消会把局部变量一起带走，
        而"掐完之后要不要保留半成品"（redirect 那一半）得看得到它们。
        """
        text_parts: list[str] = partial["text_parts"]
        tool_calls: dict[int, dict[str, str]] = partial["tool_calls"]
        tool_started = False
        async for chunk in self.llm.stream_chat(history):
            if self._pending_interrupt.get(sid) is not None:
                # 中断已在飞：立刻停手，不再发任何增量——stop 之后再冒出
                # thinking/文本残片会破坏呈现顺序。直接中止，收尾交给
                # _run_steps 的取消路径（与 task.cancel() 殊途同归）。
                raise _StepAborted()
            if chunk["type"] == "reasoning_delta":
                # 思考内容：边到边发 UI，但不进 history 也不进 log。
                # 只记一笔"吐过思考"——redirect 收尾时要靠它决定补不补空壳。
                partial["thinking_seen"] = True
                await self.bus.emit(
                    Event("agent_thinking", sid, {"text": chunk["text"]})
                )
            elif chunk["type"] == "text_delta":
                text_parts.append(chunk["text"])
                await self.bus.emit(
                    Event("agent_delta", sid, {"text": chunk["text"]})
                )
            elif chunk["type"] == "tool_call_delta":
                if not tool_started:
                    # 第一个工具增量到达即宣布"模型要调工具了"——此刻 step
                    # 还在飞（后面还有增量、工具执行、下一个 step），
                    # demo 用它做确定性插话/中断时机。
                    tool_started = True
                    await self.bus.emit(Event("tool_call_started", sid, {}))
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

        工具结果不在 task 里直接写 history——step 成功时由 turn 协程统一
        append，保证 history 里出现的是"完整 assistant 消息 + 紧随的 tool
        结果"这种合法序列。

        Stage 3 的工具默认不可中断：中断落在工具执行这一段时，正在跑的照
        跑完，**后面没开始的补"未执行"占位、不起新的**，等这一步返回后在
        step 边界由 turn 协程收尾（见 _run_steps）。
        """
        partial["text_parts"] = []
        partial["tool_calls"] = {}
        partial["thinking_seen"] = False
        self._phase[sid] = "stream"
        msg = await self._step(sid, history, partial)
        # assistant 消息一成形就先发总线、先进 log——必须在工具执行之前：
        # 否则轨迹里会先出现 tool_result、后出现发起它的 tool_call，
        # 回放读起来就是"结果比调用先发生"。工具结果仍在下面即时进 log。
        event = Event("agent_reply", sid, {"message": msg})
        await self.bus.emit(event)
        self.log.append(event, note="tool_call" if msg.get("tool_calls") else "final")
        tool_results: list[dict[str, Any]] = []
        calls = msg.get("tool_calls") or []
        if calls:
            self._phase[sid] = "tools"
        for call in calls:
            name = call["function"]["name"]
            skipped = self._pending_interrupt.get(sid) is not None
            if skipped:
                result = NO_EXEC
            elif name not in TOOLS:
                result = f"未知工具：{name}"
            else:
                # 标记"工具正在执行"：让 on_interrupt 能当场掐死在跑的工具
                # （Stage 3 默认工具可中断——真实副作用工具应自行保证可重入/可回滚）。
                self._running_tool[sid] = True
                try:
                    result = await TOOLS[name](json.loads(call["function"]["arguments"]))
                finally:
                    self._running_tool[sid] = False
            # 工具结果一落地就进 log 和总线（不等到 turn 收尾）：真执行过的
            # 可能已经改动外部世界，即便这一步随后被取消，发生过的事实也该
            # 在轨迹里；UI 也得立刻看见"未执行"的占位。
            tool_event = Event(
                "tool_result",
                sid,
                {
                    "tool_call_id": call["id"],
                    "name": name,
                    "args": json.loads(call["function"]["arguments"]),
                    "result": result,
                    "skipped": skipped,
                },
            )
            self.log.append(
                tool_event, note="tool skipped" if skipped else "tool result"
            )
            await self.bus.emit(tool_event)
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                }
            )
        partial["tool_results"] = tool_results
        return msg, tool_results

    async def _run_turn(self, event: Event) -> None:
        sid = event.session_id
        history = self.history.setdefault(sid, [])
        if not history:
            history.append({"role": "system", "content": SYSTEM_PROMPT})
        history.append({"role": "user", "content": event.payload["text"]})
        self.log.append(event, note="turn start")
        self._turn_active[sid] = True
        try:
            await self._run_steps(sid, history)
        finally:
            # 标志只活在当前 turn 里：留在 session 上会杀错下一个 turn。
            self._turn_active[sid] = False
            self._phase.pop(sid, None)
            self._running_tool.pop(sid, None)
            self._pending_interrupt.pop(sid, None)

    async def _settle_aborted(
        self, sid: str, history: list[dict[str, Any]], partial: dict[str, Any]
    ) -> bool:
        """在飞 step 被取消/中止后的统一收尾：记 step_cancelled，再决定 turn 走向。

        返回 True 表示同一个 turn 还要继续（redirect）。stop 与 _StepAborted 两条
        路都走这里——区别只在前者由 task.cancel() 触发、后者由流里主动 raise 触发，
        目的都是"在飞这一步别再发任何东西"。"""
        self._inflight[sid] = None
        pending = self._pending_interrupt.pop(sid, None) or {
            "intent": "stop",
            "text": None,
        }
        event = Event("step_cancelled", sid, {"intent": pending["intent"]})
        self.log.append(event, note="interrupt")
        await self.bus.emit(event)
        return await self._close_after_cancel(sid, history, pending, partial)

    async def _run_steps(self, sid: str, history: list[dict[str, Any]]) -> None:
        """turn 主循环：step 边界查中断、跑一步、结算。"""
        for _ in range(MAX_STEPS):
            # step 边界：中断若落在这里（没有在飞的 step），turn 层面照样收。
            pending = self._pending_interrupt.pop(sid, None)
            if pending is not None:
                if pending.get("intent") == "redirect" and pending.get("text"):
                    # 边界转向：没有残破消息可修，就是一条普通 user 消息
                    # （语义上等于 stage 2 的 steering），turn 继续。
                    self._append_synth(
                        sid,
                        history,
                        {"role": "user", "content": str(pending["text"])},
                        "redirect",
                    )
                    await self._mark_boundary(sid, "redirect")
                    continue
                self._close_stop(sid, history)
                await self._mark_boundary(sid, "stop")
                await self._end_turn(sid, "interrupted")
                return
            await self._drain_steering(sid, history)
            partial: dict[str, Any] = {}
            step_task = asyncio.create_task(self._run_step(sid, history, partial))
            self._inflight[sid] = step_task
            try:
                msg, tool_results = await step_task
            except _StepAborted:
                # 流里主动中止（pending_interrupt 已置位，不等 cancel 时序）：
                # 与下面 task.cancel() 那条路收尾完全一致。
                if await self._settle_aborted(sid, history, partial):
                    continue
                return
            except asyncio.CancelledError:
                # 区分两种取消：step_task 被 on_interrupt 掐掉（本 turn 交给
                # 我们收尾），或者 turn 协程自己被 stop() 掐掉（继续往外抛）。
                if not step_task.cancelled():
                    raise
                if await self._settle_aborted(sid, history, partial):
                    continue          # redirect：同一个 turn 里带着纠正重发
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
        """流式输出被掐之后的收尾。返回 True 表示同一个 turn 还要继续（redirect）。

        形状按"取消那一刻已经观测到什么"定，不推断模型意图。
        """
        text = pending.get("text")
        if pending.get("intent") == "redirect" and text:
            # ①②③ 一个处理：没收到完整的 LLM 返回，就当没收到——在飞 step 的产物
            # 一律丢（可见文本、半截 tool_call 都不进 history）。半截 tool_call 的
            # arguments 断在半路、不是合法消息，也没执行过，补不了占位。
            # 唯一留痕：只吐过 thinking 时补个空壳 assistant，声明这里被打断过。
            if partial.get("thinking_seen"):
                self._append_synth(
                    sid,
                    history,
                    {"role": "assistant", "content": INTERRUPTED_SHELL},
                    "interrupted",
                )
            self._append_synth(
                sid,
                history,
                {"role": "user", "content": f"{REDIRECT_NOTE}\n\n{text}"},
                "redirect",
            )
            return True
        # 掐在飞这一路不再发 turn_interrupted——那个事件的定义是"step 没被取消、
        # 只命中边界"；这里命中已经由上面的 step_cancelled 记过了。
        self._close_stop(sid, history)
        await self._end_turn(sid, "interrupted")
        return False

    def _close_stop(self, sid: str, history: list[dict[str, Any]]) -> None:
        """stop 的收口形状，**只在这一处决定**——边界命中和掐在飞两条路共用。

        按**尾部角色**选封口：尾部是 `tool`（工具结果没人接）就补 assistant
        封口占位；否则补那条 user 中断标记（尾部是没被回答的 user，不补的话
        下一轮模型会把它翻出来重答）。场景 4/5 尾部必然是 `tool`，所以都补
        assistant 封口——这跟"tool 完不完整"无关，是 turn 结束要收口。
        """
        if history and history[-1].get("role") == "tool":
            self._append_synth(
                sid, history, {"role": "assistant", "content": STOP_CLOSER}, "marker"
            )
        else:
            self._append_synth(
                sid, history, {"role": "user", "content": STOP_MARKER}, "marker"
            )

    async def _mark_boundary(self, sid: str, intent: str) -> None:
        """边界命中：step 没被取消，但 turn 在这里被收掉 / 转向。"""
        payload = {"intent": intent}
        event = Event("turn_interrupted", sid, payload)
        self.log.append(event, note="boundary")
        await self.bus.emit(event)

    def _append_synth(
        self,
        sid: str,
        history: list[dict[str, Any]],
        message: dict[str, Any],
        note: str,
        tool_name: str = "",
    ) -> None:
        """把一条**合成**消息写进 history，同时在 log 里留一条对应记录。

        合成消息（中断标记、assistant 占位、纠正 user、半成品）也必须进 log，
        否则"history 是 log 的投影"就在这里断了。类型沿用投影认得的那三种
        （agent_reply / tool_result / user_input），靠 payload 里的 synthetic
        和 note 标明它不是真发生过的对话。
        """
        history.append(message)
        role = message.get("role")
        if role == "assistant":
            event = Event("agent_reply", sid, {"message": message, "synthetic": True})
        elif role == "tool":
            event = Event(
                "tool_result",
                sid,
                {
                    "tool_call_id": message.get("tool_call_id", ""),
                    "name": tool_name,
                    "result": message.get("content", ""),
                    "skipped": True,
                    "synthetic": True,
                },
            )
        else:
            event = Event(
                "user_input",
                sid,
                {"text": message.get("content", ""), "synthetic": True},
            )
        self.log.append(event, note=note)

    async def _end_turn(self, sid: str, reason: str) -> None:
        """turn 收尾：log 记一笔，同时把 turn_end 发上总线（UI 等着它）。

        reason 同时进 log 的 note 和 payload——回放时不用再靠"有没有
        step_cancelled"反推这次 turn 是怎么结束的。
        """
        event = Event("turn_end", sid, {"reason": reason})
        self.log.append(event, note=reason)
        await self.bus.emit(event)
