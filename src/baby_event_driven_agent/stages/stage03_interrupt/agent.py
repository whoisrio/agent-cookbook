"""agent：收件箱 + 常驻 worker + 可中断的在飞步骤。

在 stage02（收件箱 / steering / followup）之上新增中断能力：

- 中断不是消息，不进收件箱，不走 steering/followup 的分类——它是
  控制信号，直接作用在"正在飞的那一步"上。
- 每 session 记录当前在飞的 step task（_inflight）。on_interrupt
  收到信号时，无 await 地检查并 cancel 它——asyncio 单线程里这段
  是原子的，没有"信号残留杀错下一个 turn"的问题：cancel 作用在
  具体的 task 对象上，不在飞的 session 查不到 task，信号自然落空。
- 取消的粒度是单步，不是 worker：step 被掐掉后 turn 记 step_cancelled
  并收尾，worker 回到收件箱接着取消息，history 里已完成的步骤全部
  保留（被掐的那步的部分输出不完整，不进 history——provider 对
  消息序列的格式要求是完整的 assistant 消息）。
- stop()（进程收尾）与中断是两回事：stop 连 worker 一起收，中断
  只动当前 step，agent 活着。
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
    ) -> None:
        self.bus = bus
        self.log = log
        self.llm = llm
        self.history: dict[str, list[dict[str, Any]]] = {}  # session_id -> messages
        self.inboxes: dict[str, asyncio.Queue[Event]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        # 每 session 当前在飞的 step task：中断的靶子。空闲时是 None。
        self._inflight: dict[str, asyncio.Task[Any] | None] = {}

    # -------------------------------------------------- 总线侧：只投递

    async def on_user_input(self, event: Event) -> None:
        """注册成总线的 user_input handler：投进收件箱立刻返回。

        put_nowait 和 spawn 检查之间没有 await，asyncio 单线程事件循环里
        这段是原子的——"消息进了队但 worker 还没起"的窗口不存在。
        """
        sid = event.session_id
        self.inboxes.setdefault(sid, asyncio.Queue()).put_nowait(event)
        if sid not in self._workers or self._workers[sid].done():
            self._workers[sid] = asyncio.create_task(self._worker(sid))

    async def on_interrupt(self, event: Event) -> None:
        """注册成总线的 user_interrupt handler：控制信号，不进收件箱。

        命中条件是"该 session 此刻有在飞的 step"。检查和 cancel 之间
        没有 await，是原子的；step 恰好刚完成（task.done()）时信号
        落空——取消请求和完成竞速，完成的赢者已定，这是真实语义。
        中断请求本身无论命中与否都进 log：它是发生过的事实。
        """
        sid = event.session_id
        task = self._inflight.get(sid)
        if task is not None and not task.done():
            task.cancel()
        self.log.append(event, note="interrupt received")

    async def stop(self) -> None:
        """进程收尾：连在飞的 step 和所有 worker 一起取消并等待退出。"""
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
            await self.bus.publish(Event("steering_consumed", sid, {"texts": texts}))
        return len(texts)

    async def _step(self, sid: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        """消费一轮流式输出：文本边到边发 agent_delta，边累积，流结束拼完整消息。"""
        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}  # index -> 累积中的调用
        tool_started = False
        async for chunk in self.llm.stream_chat(history):
            if chunk["type"] == "reasoning_delta":
                # 思考内容：边到边发 UI，但不进 history 也不进 log
                await self.bus.publish(
                    Event("agent_thinking", sid, {"text": chunk["text"]})
                )
            elif chunk["type"] == "text_delta":
                text_parts.append(chunk["text"])
                await self.bus.publish(
                    Event("agent_delta", sid, {"text": chunk["text"]})
                )
            elif chunk["type"] == "tool_call_delta":
                if not tool_started:
                    # 第一个工具增量到达即宣布"模型要调工具了"——此刻 step
                    # 还在飞（后面还有增量、工具执行、下一个 step），
                    # demo 用它做确定性插话/中断时机。
                    tool_started = True
                    await self.bus.publish(Event("tool_call_started", sid, {}))
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
        self, sid: str, history: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """一个可中断的完整单元：消费流 + 执行工具（若有）。

        工具结果不在 task 里直接写 history——中断作废的是整个 step，
        task 成功后由 turn 协程统一 append，保证 history 里出现的
        永远是"完整的 assistant 消息 + 紧随的 tool 结果"这种合法序列。
        """
        msg = await self._step(sid, history)
        tool_results: list[dict[str, Any]] = []
        for call in msg.get("tool_calls") or []:
            name = call["function"]["name"]
            if name not in TOOLS:
                result = f"未知工具：{name}"
            else:
                result = await TOOLS[name](json.loads(call["function"]["arguments"]))
            # 工具返回在这里就进 log（而不是等 turn 协程统一 append）：
            # 工具真的执行了、可能已改动外部世界，即便这个 step 随后被
            # 中断作废，发生过的事实也该在轨迹里。history 则相反，由
            # turn 协程统一写，保证序列永远合法。
            self.log.append(
                Event(
                    "tool_result",
                    sid,
                    {"tool_call_id": call["id"], "name": name, "result": result},
                ),
                note="tool result",
            )
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                }
            )
        return msg, tool_results

    async def _run_turn(self, event: Event) -> None:
        sid = event.session_id
        history = self.history.setdefault(sid, [])
        if not history:
            history.append({"role": "system", "content": SYSTEM_PROMPT})
        history.append({"role": "user", "content": event.payload["text"]})
        self.log.append(event, note="turn start")
        for _ in range(MAX_STEPS):
            await self._drain_steering(sid, history)
            step_task = asyncio.create_task(self._run_step(sid, history))
            self._inflight[sid] = step_task
            try:
                msg, tool_results = await step_task
            except asyncio.CancelledError:
                # 区分两种取消：step_task 被 on_interrupt 掐掉（本 turn
                # 就此收尾），或者 turn 协程自己被 stop() 掐掉（继续往外抛）。
                if not step_task.cancelled():
                    raise
                self._inflight[sid] = None
                self.log.append(Event("step_cancelled", sid, {}), note="interrupt")
                await self.bus.publish(Event("step_cancelled", sid, {}))
                await self._end_turn(sid, "interrupted")
                return
            finally:
                if self._inflight.get(sid) is step_task:
                    self._inflight[sid] = None
            history.append(msg)
            await self.bus.publish(Event("agent_reply", sid, {"message": msg}))
            # 完整回答进 log（agent_delta 不记：它是传输层的瞬时增量，
            # 累积结果就是这条 reply）
            self.log.append(
                Event("agent_reply", sid, {"message": msg}),
                note="tool_call" if msg.get("tool_calls") else "final",
            )
            if not tool_results:
                await self._end_turn(sid, "turn end")
                return
            history.extend(tool_results)
        await self._end_turn(sid, "max steps")

    async def _end_turn(self, sid: str, note: str) -> None:
        """turn 收尾：log 记一笔，同时把 turn_end 发上总线（UI 等着它）。"""
        event = Event("turn_end", sid, {})
        self.log.append(event, note=note)
        await self.bus.publish(event)
