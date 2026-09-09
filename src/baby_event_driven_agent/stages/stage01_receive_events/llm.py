"""LLM 客户端与工具。

RealLLM 是默认：OpenAI 兼容的流式客户端，配置读仓库根 .env
（OPENAI_API_BASE / OPENAI_API_KEY / OPENAI_MODEL，环境变量优先于文件）。
FakeLLM 只给 tests 用——tests 要确定性、不花钱、不依赖网络。
两者共享同一个 stream_chat 增量协议，agent 代码对二者无感。

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

    按"最后一条消息"路由（不能看整个 history 的角色分布——那样第二轮
    会带着第一轮的 tool 记录跳过检索）。arguments 故意拆成两块发，
    逼 agent 的增量累积逻辑真实工作，而不是只测理想路径。
    """

    async def stream_chat(
        self, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        last = messages[-1]
        if last.get("role") == "tool":
            for piece in ("根据检索结果", "回答：", last.get("content", "")):
                yield {"type": "text_delta", "text": piece}
            return
        text = last.get("content") or ""
        if "杯" in text:
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
        for piece in ("已", "记录。"):
            yield {"type": "text_delta", "text": piece}
