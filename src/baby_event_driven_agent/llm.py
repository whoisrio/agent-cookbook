"""LLM 接入。

demo 默认用 ScriptedLLM：按脚本返回，不需要 API key，要验的是事件机制
不是模型。接真实模型时用 OpenAIChatLLM，它只依赖 openai 包，且是 lazy
import——没装也能跑 demo。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from .agent import AssistantMessage, CancelSignal, ToolCall, new_id

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


class ScriptedLLM:
    """按脚本回放的假模型。

    script 里每一项是一轮的回复：
      {"content": "..."}                      直接说完
      {"tool_calls": [{"name": ..., "args": {...}}]}  要调工具
      {"sleep": 3.0, "content": "..."}         故意慢，用来演示中断
    """

    def __init__(self, script: list[dict[str, Any]] | None = None, delay: float = 0.0):
        self.script = list(script or [])
        self.delay = delay
        self._turn = 0
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cancel: CancelSignal | None = None,
    ) -> AssistantMessage:
        self.calls += 1
        if self.delay:
            await _sleep_or_cancel(self.delay, cancel)

        item = self.script[self._turn] if self._turn < len(self.script) else {}
        self._turn += 1

        if "sleep" in item:
            await _sleep_or_cancel(item["sleep"], cancel)

        tool_calls = tuple(
            ToolCall(
                id=f"call_{self.calls}_{i}",
                name=spec["name"],
                arguments=json.dumps(spec.get("args", {}), ensure_ascii=False),
            )
            for i, spec in enumerate(item.get("tool_calls", []))
        )
        return AssistantMessage(content=item.get("content"), tool_calls=tool_calls)


class OpenAIChatLLM:
    """真实模型。走 Chat Completions，国内 provider 基本都兼容。

    默认流式：一是能增量拿到内容，二是最重要的——**只有流式才能真正
    取消**。非流式下一次调用就是一个 HTTP 请求，取消只能把连接掐断，
    已生成的内容全丢；流式下可以在任意 chunk 处停手，把流关掉，服务
    端看到断连就停止生成。
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        *,
        stream: bool = True,
    ) -> None:
        from dotenv import dotenv_values  # lazy import：没装也能跑 demo
        from openai import AsyncOpenAI

        config = dotenv_values(os.path.join(_REPO_ROOT, ".env"))
        self.model = model or config.get("OPENAI_MODEL") or "gpt-4o-mini"
        self._stream = stream
        self._client = AsyncOpenAI(
            api_key=config.get("OPENAI_API_KEY"),
            base_url=base_url or config.get("OPENAI_API_BASE"),
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cancel: CancelSignal | None = None,
    ) -> AssistantMessage:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": self._stream,
        }
        if tools:
            kwargs["tools"] = tools

        if not self._stream:
            # 非流式没有检查点，只能靠外层 task.cancel() 掐断连接
            response = await self._client.chat.completions.create(**kwargs)
            message = response.choices[0].message
            return AssistantMessage(
                content=message.content,
                tool_calls=tuple(
                    ToolCall(
                        id=c.id,
                        name=c.function.name,
                        arguments=c.function.arguments,
                    )
                    for c in (message.tool_calls or [])
                ),
            )
        return await self._chat_stream(kwargs, cancel)

    async def _chat_stream(
        self, kwargs: dict[str, Any], cancel: CancelSignal | None
    ) -> AssistantMessage:
        """攒流式增量。tool_calls 是分片来的：id / name / arguments 都要
        按 index 拼，直接取第一个 delta 会拿到半截 JSON。"""
        stream = await self._client.chat.completions.create(**kwargs)
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        try:
            async for chunk in stream:
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError()
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    content.append(delta.content)
                for piece in delta.tool_calls or ():
                    buf = calls.setdefault(
                        piece.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if piece.id:
                        buf["id"] += piece.id
                    if piece.function:
                        if piece.function.name:
                            buf["name"] += piece.function.name
                        if piece.function.arguments:
                            buf["arguments"] += piece.function.arguments
        finally:
            # 取消也要走到这里，否则连接留在半开状态
            await stream.close()

        return AssistantMessage(
            content="".join(content) or None,
            tool_calls=tuple(
                ToolCall(
                    # id 是工具结果配对的键，provider 不给就自己造一个
                    id=buf["id"] or new_id("call"),
                    name=buf["name"],
                    arguments=buf["arguments"],
                )
                for buf in calls.values()
            ),
        )


async def _sleep_or_cancel(seconds: float, cancel: CancelSignal | None) -> None:
    """假模型的"耗时长调用"。切成小片睡，好在每片之间看一眼取消信号。"""
    if cancel is None:
        await asyncio.sleep(seconds)
        return
    end = time.monotonic() + seconds
    while True:
        if cancel.is_set():
            raise asyncio.CancelledError()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.05))
