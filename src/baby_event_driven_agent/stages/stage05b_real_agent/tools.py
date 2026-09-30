"""工具层（05b）：从 04 继承六件套，search_rules / update_rules 换知识库后端。

模型侧看到的还是 search_rules / update_rules——换的是工具背后的世界：

1. **search_rules**：后端从逐行子串匹配换成知识库 top-k 检索（双路：
   语义优先、词面兜底，mode 如实标注），schema 只加可选 top_k。
2. **update_rules**：语义从"按标题改 rules.txt 一行"升级为"按标题重写
   整篇知识库文档"（设值语义，与 update_inventory 同一纪律）→ 重切该
   文档的块 → 原地更新索引 → 立即可检索——知识运营闭环。

其余四件（query/update_inventory、list/get_task）连同判量审批、设值
语义、数据隔离直接继承 stage04 的实现，一行不改——机制分层的收益：
前面各章的机制（审批、治理、轨迹、压缩）不因工具后端升级而动。

数据源：包自带 data/knowledge/（源文档）与 data/knowledge_index.json
（物化索引）。两个模块级 Path 可被测试 / demo 换成工作目录副本，
写操作的真写落点，包自带文件一个字节不动。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from . import knowledge as kb
from baby_event_driven_agent.stages.stage04_trajectory import tools as base_tools
from baby_event_driven_agent.stages.stage04_trajectory.llm import RealLLM as _BaseRealLLM

# 数据源：默认本包 data/，测试 / demo 可整体换成工作目录副本
_DATA = Path(__file__).resolve().parent / "data"
_KNOWLEDGE_DIR = _DATA / "knowledge"
_INDEX_PATH = _DATA / "knowledge_index.json"

TOP_K_MIN, TOP_K_MAX = 1, 10


# ---------------------------------------------------------------- 检索（读）


async def search_rules(args: dict[str, Any]) -> str:
    """知识库 top-k 检索：语义优先、词面兜底，命中带出处与分数。"""
    query = str(args.get("query", "")).strip()
    if not query:
        return "缺少 query，未检索"
    try:
        top_k = int(args.get("top_k", kb.DEFAULT_TOP_K))
    except (TypeError, ValueError):
        top_k = kb.DEFAULT_TOP_K
    top_k = max(TOP_K_MIN, min(top_k, TOP_K_MAX))
    index = kb.KnowledgeIndex(_KNOWLEDGE_DIR, _INDEX_PATH, kb.get_embedder())
    await index.ensure()
    return kb.format_result(await index.search(query, top_k))


# ---------------------------------------------------------------- 写路径


def _doc_filename(title: str) -> str:
    """标题 → 安全文件名：路径分隔符等一律换成连字符。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "-", title.strip()).strip("-.")
    return name or "untitled"


async def update_rules(args: dict[str, Any]) -> str:
    """按标题重写整篇知识库文档（设值语义）→ 增量更新索引 → 立即可检索。

    设值语义与 update_inventory 同一纪律：写的是目标状态不是增量，
    rewind / 重放之后哪怕重做，结果不叠加。content 支持整篇 markdown
    （带 `##` 节标题）；不带标题行的纯文本会自动冠上 `# {title}`。
    """
    title = str(args.get("title", "")).strip()
    content = str(args.get("content", "")).strip()
    if not title or not content:
        return "需要 title 和 content，未执行"
    filename = f"{_doc_filename(title)}.md"
    raw = content if content.lstrip().startswith("# ") else f"# {title}\n\n{content}"
    path = _KNOWLEDGE_DIR / filename
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw.rstrip() + "\n", encoding="utf-8")

    index = kb.KnowledgeIndex(_KNOWLEDGE_DIR, _INDEX_PATH, kb.get_embedder())
    await index.ensure()
    await index.update_document(filename, raw)
    n_chunks = sum(1 for c in index.chunks() if c.source == filename)
    verb = "已更新" if existed else "已写入"
    return f"{verb}知识库文档《{title}》（{n_chunks} 块），立即可检索"


# ---------------------------------------------------------------- 工具表


def _fn_schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _inherited() -> dict[str, base_tools.Tool]:
    """04 六件套里的四件原样继承（同一个 Tool 对象：schema / fn / 审批声明）。"""
    return {
        name: tool
        for name, tool in base_tools.TOOLS.items()
        if name not in ("search_rules", "update_rules")
    }


# 工具描述就是给模型的路由依据：检索词怎么给、写制度意味着什么，
# 说得越清楚，模型选错工具 / 用错粒度的概率越低。
TOOLS: dict[str, base_tools.Tool] = {
    **_inherited(),
    "search_rules": base_tools.Tool(
        schema=_fn_schema(
            "search_rules",
            "检索团队知识库（补货制度、盘点差异处理、报销流程、会议室预订等文档），"
            "返回最相关的知识块及其出处",
            {
                "query": {
                    "type": "string",
                    "description": "要查的问题或关键词，用自然语言描述即可",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回的块数上限，默认 5",
                },
            },
            ["query"],
        ),
        fn=search_rules,
    ),
    "update_rules": base_tools.Tool(
        schema=_fn_schema(
            "update_rules",
            "把一条制度 / 规则按标题记进知识库：同标题的文档会被整篇重写为给定内容"
            "（设值语义，不是追加）",
            {
                "title": {"type": "string", "description": "制度 / 文档标题，如：盘点差异处理"},
                "content": {"type": "string", "description": "制度内容（整篇重写后的全文）"},
            },
            ["title", "content"],
        ),
        fn=update_rules,
    ),
}

# RealLLM 发请求用的 schema 列表；system prompt 从 schema 生成（工具的分工
# 只写在 description 一处，与 04 同一份纪律）
TOOL_SCHEMAS: list[dict[str, Any]] = [t.schema for t in TOOLS.values()]
build_system_prompt = base_tools.build_system_prompt


class RealLLM(_BaseRealLLM):
    """同 04 的 RealLLM，默认工具表换成 05b 的（search_rules 带 top_k）。

    摘要器显式传 tools=None 时透传 None（裸 chat），不夹带工具表。
    """

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = TOOL_SCHEMAS,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for chunk in super().stream_chat(messages, tools=tools, max_tokens=max_tokens):
            yield chunk
