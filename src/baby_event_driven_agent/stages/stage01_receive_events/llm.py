"""LLM 客户端与工具。

RealLLM 是唯一的 LLM：OpenAI 兼容的流式客户端，配置读仓库根 .env
（OPENAI_API_BASE / OPENAI_API_KEY / OPENAI_MODEL，环境变量优先于文件）。
指向本地 ollama（http://localhost:11434/v1 + qwen3.5:4b-32k）或云上端点都行，
demo 和 tests 用同一个客户端——tests 依赖本地模型可达，不可达就跳过。

增量协议（归一化 chunk）：
- {"type": "text_delta", "text": str}       一段可见文本增量
- {"type": "reasoning_delta", "text": str}  一段思考内容增量（同样流向 UI，
                                             只是不进 session log）
- {"type": "tool_call_delta", "index": int, 工具调用增量：首块带 id / name，
   "id": str | None, "name": str | None,     arguments 常分多块到达，必须累积
   "args_delta": str}
流结束即本轮请求结束。

工具四件套，读写成对：
- 读数据 query_inventory / 写数据 update_inventory——文章叙述里它们扮演
  业务系统的接口，代码上用 inventory.txt 模拟这个业务库。
- 读规则 search_rules / 写规则 update_rules——扮演 RAG 检索与知识运营，
  代码上用 rules.txt 模拟规则库（逐行关键词匹配，不是真 RAG）。
写操作真写文件：写完再读，数据真的变了，这就是"工具动了外部世界"。
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

# 知识库三个 stage 共享：inventory.txt 模拟业务库，rules.txt 模拟规则库
_KB = Path(__file__).resolve().parents[2] / "knowledge-base"
_INVENTORY = _KB / "inventory.txt"
_RULES = _KB / "rules.txt"


def _read_lines(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


async def query_inventory(args: Args) -> str:
    """品类库存查询：命中返回该品类一行；没命中返回现有品类清单。"""
    category = str(args.get("category", "")).strip()
    lines = _read_lines(_INVENTORY)
    hits = [ln for ln in lines if category in ln]
    if hits:
        return "\n".join(hits)
    names = "、".join(ln.split("：", 1)[0] for ln in lines)
    return f"未收录该品类。现有品类：{names}"


async def update_inventory(args: Args) -> str:
    """添加新品类或更新已有品类的库存与规格，真写 inventory.txt。"""
    category = str(args.get("category", "")).strip()
    stock = args.get("stock", 0)
    spec = str(args.get("spec", "")).strip().rstrip("。")
    if not category:
        return "缺少 category，未执行"
    new_line = (
        f"{category}：库存 {stock} 件；{spec}。" if spec else f"{category}：库存 {stock} 件。"
    )
    lines = _read_lines(_INVENTORY)
    out: list[str] = []
    replaced = False
    for ln in lines:
        if ln.split("：", 1)[0] == category:
            out.append(new_line)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(new_line)
    _INVENTORY.write_text("\n".join(out) + "\n", encoding="utf-8")
    return f"{'已更新' if replaced else '已添加'}：{new_line}"


async def search_rules(args: Args) -> str:
    """规则库检索：逐行匹配，返回命中的规则条目。

    先按空格分词匹配；整句分不出词（没有空格）就退化成 2 字滑窗，
    命中两个以上片段才算。
    """
    query = str(args.get("query", "")).strip()
    lines = _read_lines(_RULES)
    terms = [t for t in query.split() if t] or ([query] if query else [])
    hits = [ln for ln in lines if any(t in ln for t in terms)]
    if not hits and len(query) > 2:
        grams = [query[i : i + 2] for i in range(len(query) - 1)]
        hits = [ln for ln in lines if sum(g in ln for g in grams) >= 2]
    return "\n".join(hits) if hits else "（无命中）"


async def update_rules(args: Args) -> str:
    """按标题添加或更新一条规则（存在则整行替换），真写 rules.txt。"""
    title = str(args.get("title", "")).strip()
    content = str(args.get("content", "")).strip()
    if not title or not content:
        return "需要 title 和 content，未执行"
    new_line = f"{title}：{content.rstrip('。')}。"
    lines = _read_lines(_RULES)
    out: list[str] = []
    replaced = False
    for ln in lines:
        if ln.split("：", 1)[0] == title:
            out.append(new_line)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(new_line)
    _RULES.write_text("\n".join(out) + "\n", encoding="utf-8")
    return f"{'已更新' if replaced else '已添加'}：{new_line}"


# 工具描述就是给模型的路由依据：查什么数据、查什么规则、什么时候写，
# 写得越清楚，模型选错工具的概率越低。
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_inventory",
            "description": "查询品类库存与规格（业务数据）。品类名如：保温杯、玻璃杯",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "品类名"}
                },
                "required": ["category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_inventory",
            "description": "添加新品类，或更新某品类的库存数量与规格",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "品类名"},
                    "stock": {"type": "integer", "description": "库存件数"},
                    "spec": {"type": "string", "description": "规格描述，可省略"},
                },
                "required": ["category", "stock"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_rules",
            "description": "检索团队规则、流程、制度（如会议室预订、VPN 申请、报销）",
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
    },
    {
        "type": "function",
        "function": {
            "name": "update_rules",
            "description": "添加一条新规则，或按标题更新已有规则的内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "规则标题，如：报销"},
                    "content": {"type": "string", "description": "规则内容"},
                },
                "required": ["title", "content"],
            },
        },
    },
]

TOOLS: dict[str, ToolFn] = {
    "query_inventory": query_inventory,
    "update_inventory": update_inventory,
    "search_rules": search_rules,
    "update_rules": update_rules,
}


def build_system_prompt(schemas: list[dict[str, Any]] | None = None) -> str:
    """system prompt 从 tool schemas 生成：工具的分工只写在 description 一处。

    手写路由表（"查X用toolA、查Y用toolB"）会和 description 重复，
    且加一个工具就得回来改 prompt。这里把 name + description 列成清单，
    description 改了 prompt 自动跟上。不用完全放手的原因见 agent.py。
    """
    schemas = schemas if schemas is not None else TOOL_SCHEMAS
    lines = ["你是智能助手，必须基于事实来回答用户的提问，**严禁编造**，得不到事实，就回答不知道。可用工具："]
    for s in schemas:
        fn = s["function"]
        params = "、".join(fn["parameters"].get("properties", {}))
        lines.append(f"- {fn['name']}：{fn['description']}" + (f"（参数：{params}）" if params else ""))
    lines.append(
        "必须基于事实回答用户问题。用户的问题或请求涉及上面某个工具时，"
        "选对工具、先拿到真实结果再回答；获取不到准确信息就回答不知道，严禁编造。"
        "用户要求记录或修改时，用对应的写工具落库，然后一句话确认改了什么。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 客户端


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
        print(f'###【start conversation】: Using {self.model} in this conversation...\n')
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
