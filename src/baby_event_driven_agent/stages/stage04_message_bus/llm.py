"""LLM 客户端（工具定义在 tools.py——工具的事归工具）。

RealLLM：OpenAI 兼容的流式客户端，配置读仓库根 .env，环境变量优先。
本章的传输层测试大多不需要模型（总线 / 治理都可以离线验），
只有“真跑一轮”的用例打真模型。

增量协议（归一化 chunk）：
- {"type": "text_delta", "text": str}       一段可见文本增量
- {"type": "tool_call_delta", "index": int, 工具调用增量：首块带 id / name，
   "id": str | None, "name": str | None,     arguments 常分多块到达，必须累积
   "args_delta": str}
流结束即本轮请求结束。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from openai import AsyncOpenAI

from .tools import TOOL_SCHEMAS

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIG = dotenv_values(_REPO_ROOT / ".env")


def _cfg(key: str) -> str:
    """环境变量优先于 .env 文件——临时换模型不用改文件。"""
    return os.environ.get(key) or _CONFIG.get(key) or ""


class RealLLM:
    """OpenAI 兼容流式客户端，stream_chat 产出归一化增量块。"""

    def __init__(self) -> None:
        api_key = _cfg("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "缺少 OPENAI_API_KEY：写在仓库根 .env 或环境变量里"
                "（本地 ollama 填任意非空值即可）"
            )
        self.model = _cfg("OPENAI_MODEL")
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=_cfg("OPENAI_API_BASE") or None,
            timeout=120.0,
        )

    async def stream_chat(
        self, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        stream = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # 思考内容单独成一路，不跳过：云上 qwen3 系列走 reasoning_content，
            # 本地 ollama 走 reasoning，归一化成 reasoning_delta 发给 UI，
            # 不和可见文本混流。
            extra = delta.model_extra or {}
            thinking = extra.get("reasoning_content") or extra.get("reasoning")
            if thinking:
                yield {"type": "reasoning_delta", "text": thinking}
            if delta.content:
                yield {"type": "text_delta", "text": delta.content}
            for tc in delta.tool_calls or []:
                fn = tc.function
                yield {
                    "type": "tool_call_delta",
                    "index": tc.index,
                    "id": tc.id or None,
                    "name": fn.name if fn else None,
                    "args_delta": (fn.arguments if fn else "") or "",
                }
