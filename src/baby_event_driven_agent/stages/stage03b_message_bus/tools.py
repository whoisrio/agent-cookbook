"""工具定义：名字、schema、实现、审批要求——工具的事全在工具身上。

总线上的治理（拦截者）判的是"现在能不能执行"（黑名单、规则匹配，当场）；
"要不要先问人"不归总线管——`requires_approval` 声明在工具自己身上，
agent 执行前查一次声明，要问就走人工确认流程。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

Args = dict[str, Any]
ToolFn = Any  # async (Args) -> str

# 知识库各 stage 共享：inventory.txt 模拟业务库，rules.txt 模拟规则库
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


@dataclass(frozen=True)
class Tool:
    """一个工具的完整定义：给模型看的 schema、真实现、要不要人工确认。

    审批要求声明在工具自己身上（和事件类声明消费约束同一个道理）：
    总线与订阅者不知道哪个工具要审批，agent 执行前查一次声明。
    """

    schema: dict[str, Any]  # function schema：name / description / parameters
    fn: ToolFn
    requires_approval: bool = False  # 执行前要不要问人
    approval_reason: str = ""  # 问人时给用户看的理由


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


# 工具描述就是给模型的路由依据：查什么数据、查什么规则、什么时候写，
# 写得越清楚，模型选错工具的概率越低。
TOOLS: dict[str, Tool] = {
    "query_inventory": Tool(
        schema=_fn_schema(
            "query_inventory",
            "查询品类库存与规格（业务数据）。品类名如：保温杯、玻璃杯",
            {"category": {"type": "string", "description": "品类名"}},
            ["category"],
        ),
        fn=query_inventory,
    ),
    "update_inventory": Tool(
        schema=_fn_schema(
            "update_inventory",
            "添加新品类，或更新某品类的库存数量与规格",
            {
                "category": {"type": "string", "description": "品类名"},
                "stock": {"type": "integer", "description": "库存件数"},
                "spec": {"type": "string", "description": "规格描述，可省略"},
            },
            ["category", "stock"],
        ),
        fn=update_inventory,
        requires_approval=True,  # 真写业务数据：执行前问人
        approval_reason="该工具会修改库存数据，需要人工确认",
    ),
    "search_rules": Tool(
        schema=_fn_schema(
            "search_rules",
            "检索团队规则、流程、制度（如会议室预订、VPN 申请、报销）",
            {
                "query": {
                    "type": "string",
                    "description": "检索词，多个关键词用空格分隔",
                }
            },
            ["query"],
        ),
        fn=search_rules,
    ),
    "update_rules": Tool(
        schema=_fn_schema(
            "update_rules",
            "添加一条新规则，或按标题更新已有规则的内容",
            {
                "title": {"type": "string", "description": "规则标题，如：报销"},
                "content": {"type": "string", "description": "规则内容"},
            },
            ["title", "content"],
        ),
        fn=update_rules,
    ),
}

# RealLLM 发请求用的 schema 列表
TOOL_SCHEMAS: list[dict[str, Any]] = [t.schema for t in TOOLS.values()]


def build_system_prompt(schemas: list[dict[str, Any]] | None = None) -> str:
    """system prompt 从 tool schemas 生成：工具的分工只写在 description 一处。"""
    schemas = schemas if schemas is not None else TOOL_SCHEMAS
    lines = ["你是一个通过工具干活的通用 agent。可用工具："]
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
