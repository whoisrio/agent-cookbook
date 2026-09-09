"""LLM 客户端与工具（与 stage01 同构，RealLLM / search 未变）。

FakeLLM 比 stage01 多一个 first_call_gate：第一步请求开始时等这个
Event——tests 用它把"消息在 step 在飞时到达"变成确定性的时序，
否则 steering 只能靠 sleep 碰运气。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from openai import AsyncOpenAI

Args = dict[str, Any]
ToolFn = Any  # async (Args) -> str

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIG = dotenv_values(_REPO_ROOT / ".env")


def _cfg(key: str) -> str:
    """环境变量优先于 .env 文件——临时换模型不用改文件。"""
    return os.environ.get(key) or _CONFIG.get(key) or ""


# ---------------------------------------------------------------- 工具

_KNOWLEDGE = Path(__file__).resolve().parent / "knowledge.txt"


async def search(args: Args) -> str:
    """本地知识库检索：逐行匹配，返回命中的条目行。

    先按空格分词匹配；整句分不出词（没有空格）就退化成 2 字滑窗，
    命中两个以上片段才算。
    """
    query = str(args.get("query", "")).strip()
    lines = [
        ln.strip()
        for ln in _KNOWLEDGE.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    terms = [t for t in query.split() if t] or ([query] if query else [])
    hits = [ln for ln in lines if any(t in ln for t in terms)]
    if not hits and len(query) > 2:
        grams = [query[i : i + 2] for i in range(len(query) - 1)]
        hits = [ln for ln in lines if sum(g in ln for g in grams) >= 2]
    return "\n".join(hits) if hits else "（无命中）"


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "在本地知识库中检索，返回命中的条目行",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索词，多个关键词用空格分隔",
                    }
                },
                "required": ["query"],
            },
        },
    }
]

TOOLS: dict[str, ToolFn] = {"search": search}


# ---------------------------------------------------------------- 客户端


class RealLLM:
    """OpenAI 兼容流式客户端，stream_chat 产出归一化增量块。"""

    def __init__(self) -> None:
        api_key = _cfg("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "缺少 OPENAI_API_KEY：写在仓库根 .env 或环境变量里"
            )
        self.model = _cfg("OPENAI_MODEL")
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=_cfg("OPENAI_API_BASE") or None,
            timeout=60.0,
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
            # qwen3 系列思考内容走 reasoning_content，不是面向用户的输出，跳过
            if (delta.model_extra or {}).get("reasoning_content"):
                continue
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


class FakeLLM:
    """与 RealLLM 同协议的离线替身，只给 tests 用。

    first_call_gate 不为 None 时，第一次 stream_chat 在产出任何增量前
    等待该 Event——测试先让 step 飞起来，再往 inbox 投消息，
    steering 的时序就是确定的而不是 sleep 碰运气。
    first_call_tool 为 True 时第一次调用发起 search（把 turn 撑成两步，
    中间才有 step 边界给 steering 用）。
    """

    def __init__(
        self,
        first_call_gate: asyncio.Event | None = None,
        first_call_tool: bool = False,
    ) -> None:
        self.first_call_gate = first_call_gate
        self.first_call_tool = first_call_tool
        self.calls = 0

    async def stream_chat(
        self, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        if self.calls == 1:
            if self.first_call_gate is not None:
                await self.first_call_gate.wait()
            if self.first_call_tool:
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": "c1",
                    "name": "search",
                    "args_delta": '{"query": ',
                }
                yield {
                    "type": "tool_call_delta",
                    "index": 0,
                    "id": None,
                    "name": None,
                    "args_delta": '"杯子"}',
                }
                return
        last = messages[-1]
        if last.get("role") == "tool":
            for piece in ("根据检索结果", "回答：", last.get("content", "")):
                yield {"type": "text_delta", "text": piece}
            return
        text = last.get("content") or ""
        yield {"type": "text_delta", "text": f"回复：{text}"}
