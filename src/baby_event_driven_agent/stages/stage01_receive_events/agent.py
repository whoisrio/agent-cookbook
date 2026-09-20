"""agent 主循环：拉 user_input，跑一轮（模型 → 工具 → 模型 → 回话）。

模型输出是流式的：文本增量边到边经总线发给 UI（agent_delta 事件）；
工具调用增量边到边累积（arguments 常分多块到达），流结束后才拼出
完整的 assistant 消息。整个 turn 仍在总线回调里跑完——一次处理
一个请求是本 stage 的能力边界，不是缺陷，排队和打断是 Stage 2 / 3 的需求。
"""

from __future__ import annotations

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

    async def on_user_input(self, event: Event) -> None:
        """注册成总线的 user_input handler，原地跑整个 turn。"""
        await self._run_turn(event)

    async def _step(self, sid: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        """消费一轮流式输出：文本边到边发 agent_delta，边累积，流结束拼完整消息。"""
        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}  # index -> 累积中的调用
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
            msg = await self._step(sid, history)
            history.append(msg)
            await self.bus.publish(Event("agent_reply", sid, {"message": msg}))
            # 完整回答进 log（agent_delta 不记：它是传输层的瞬时增量，
            # 累积结果就是这条 reply）
            self.log.append(
                Event("agent_reply", sid, {"message": msg}),
                note="tool_call" if msg.get("tool_calls") else "final",
            )
            if "tool_calls" not in msg:
                self.log.append(Event("turn_end", sid, {}), note="turn end")
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
                tool_event = Event(
                    "tool_result",
                    sid,
                    {"tool_call_id": call["id"], "name": name, "result": result},
                )
                self.log.append(tool_event, note="tool result")
                # 结果也发上总线：demo 的 UI 靠它打出「执行工具」一行。
                await self.bus.publish(tool_event)
        self.log.append(Event("turn_end", sid, {}), note="max steps")
