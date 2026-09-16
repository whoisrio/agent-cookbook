"""agent：收件箱 + 常驻 worker，steering 在 step 边界生效。

本章总线分了方向：inbound 的 publish 是同步的，按 agent_id 把命令交给
`enqueue`；outbound 的事件改走 `emit`。

`enqueue` 只做一件事——把事件投进对应 session 的收件箱就返回，turn 不再
占着总线回调。每个 session 一个收件箱加一个常驻 worker task（第一次收到
该 session 的消息时启动），worker 循环"取消息 → 跑 turn"，turn 结束回到
取消息——排在 turn 之后的消息（followup）就是下一次 get 到的东西。

消息语义是消费那一刻定的，不是提交时定的：
- worker 空闲时取到 → 新 turn 的输入（followup）
- turn 在跑、step 边界 drain 到 → 拼进当前上下文继续走（steering）
- 同一条消息，落在哪个窗口就是什么，自己不背语义

worker 永不自行退出（只响应 stop 的 cancel），所以"消息进队之后
task 恰好死掉"的竞态在这个设计里根本不存在——退出清理是 stop 的
显式职责，不是每个 turn 的尾部负担。
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
        agent_id: str = "agent",
    ) -> None:
        self.bus = bus
        self.log = log
        self.llm = llm
        self.agent_id = agent_id
        self.history: dict[str, list[dict[str, Any]]] = {}  # session_id -> messages
        self.inboxes: dict[str, asyncio.Queue[Event]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        # inbound 路由：publish(event, to=agent_id) 会落到这个投递函数
        bus.register(agent_id, self.enqueue)

    # -------------------------------------------------- inbound 侧：只投递

    def enqueue(self, event: Event) -> None:
        """总线 inbound 的投递函数（**同步**）：投进收件箱立刻返回。

        同步、且方法体内没有任何 await，整段就是原子的——put_nowait 和
        spawn 检查之间不可能被插入，"消息进了队但 worker 还没起"的窗口
        不存在。调用方也 await 不到它，更 create_task 不了它。
        """
        sid = event.session_id
        self.inboxes.setdefault(sid, asyncio.Queue()).put_nowait(event)
        if sid not in self._workers or self._workers[sid].done():
            self._workers[sid] = asyncio.create_task(self._worker(sid))

    async def stop(self) -> None:
        """显式收尾：取消所有 worker 并等它们退出。"""
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

    async def _step(self, sid: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        """消费一轮流式输出：文本边到边发 agent_delta，边累积，流结束拼完整消息。"""
        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}  # index -> 累积中的调用
        tool_started = False
        async for chunk in self.llm.stream_chat(history):
            if chunk["type"] == "reasoning_delta":
                # 思考内容：边到边发 UI，但不进 history 也不进 log
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
                    # demo 用它做确定性插话时机：必然赶得上下一个 step 边界。
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

    async def _run_turn(self, event: Event) -> None:
        sid = event.session_id
        history = self.history.setdefault(sid, [])
        if not history:
            history.append({"role": "system", "content": SYSTEM_PROMPT})
        history.append({"role": "user", "content": event.payload["text"]})
        self.log.append(event, note="turn start")
        for _ in range(MAX_STEPS):
            await self._drain_steering(sid, history)
            msg = await self._step(sid, history)
            history.append(msg)
            await self.bus.emit(Event("agent_reply", sid, {"message": msg}))
            # 完整回答进 log（agent_delta 不记：它是传输层的瞬时增量，
            # 累积结果就是这条 reply）
            self.log.append(
                Event("agent_reply", sid, {"message": msg}),
                note="tool_call" if msg.get("tool_calls") else "final",
            )
            if "tool_calls" not in msg:
                await self._end_turn(sid, "turn end")
                return
            for call in msg["tool_calls"]:
                name = call["function"]["name"]
                if name not in TOOLS:
                    result = f"未知工具：{name}"
                else:
                    result = await TOOLS[name](
                        json.loads(call["function"]["arguments"])
                    )
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result,
                    }
                )
                # 工具返回进 log：它是模型下一轮 context 的一部分，不记的话
                # 轨迹中间是断的，"模型为什么这么答"无从分析。工具真的执行了
                # （可能已改动外部世界），发生过的事实就该在轨迹里。
                self.log.append(
                    Event(
                        "tool_result",
                        sid,
                        {"tool_call_id": call["id"], "name": name, "result": result},
                    ),
                    note="tool result",
                )
        await self._end_turn(sid, "max steps")

    async def _end_turn(self, sid: str, note: str) -> None:
        """turn 收尾：log 记一笔，同时把 turn_end 发上总线（UI 等着它）。"""
        event = Event("turn_end", sid, {})
        self.log.append(event, note=note)
        await self.bus.emit(event)
