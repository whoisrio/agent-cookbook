"""工具层：六件套（4 读 2 写）+ 声明式审批（判量）。

从四件套（query/update_inventory、search/update_rules）升级两步：

1. **任务域 +2**：list_tasks / get_task。任务单是只读输入，没有状态字段——
   "做到哪了"只存在于会话里。这是刻意的：压缩（04）要保的正是这个进度；
   一旦有状态文件，agent 压完重读状态就行，摘要的职责就被架空了。
2. **审批声明从"判工具"到"判量"**：`requires_approval: bool` 升级为
   `approval_check`（参数 → 问人的理由或 None）。审批要求仍然声明在工具
   自己身上（03b 起的纪律），只是细化成"什么量才要问"。update_inventory
   声明的线和 data/rules.txt 里的补货规则是同一条：业务规则告诉模型
   "超 50 要报备"，工具声明告诉 harness"超 50 要问人"。

数据源自带 `data/`（inventory / rules / tasks，副本加扩充），共享的
knowledge-base 一个字节不动——前面各章的测试断言了它的具体值。
三个模块级 Path 可被测试 / demo 换成工作目录副本（写操作的真写落点）。

写操作保持**设值语义**（写目标值不是增量）：rewind / 重放之后哪怕重做，
"补到 50"还是 50，副作用不叠加——这是 04 轨迹剧情的安全前提。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

Args = dict[str, Any]
ToolFn = Any  # async (Args) -> str

# 数据源：默认本包 data/，测试 / demo 可整体换成工作目录副本
_DATA = Path(__file__).resolve().parent / "data"
_INVENTORY = _DATA / "inventory.txt"
_RULES = _DATA / "rules.txt"
_TASKS = _DATA / "tasks.txt"


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
    """添加新品类或更新已有品类的库存与规格，真写 inventory.txt（设值语义）。"""
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


# ---------------------------------------------------------------- 任务域


async def list_tasks(args: Args) -> str:
    """列出今天的任务单：任务号、类型、条目数。

    任务单只读、没有状态字段——"做到哪了"住在会话里，不住在文件里。
    文件格式：不带"："的行是任务头（id 标题 说明），带"："的行是条目。
    """
    lines = _read_lines(_TASKS)
    headers = [ln for ln in lines if "：" not in ln]
    if not headers:
        return "（今天没有任务单）"
    out: list[str] = []
    for h in headers:
        parts = h.split(" ", 2)
        tid = parts[0]
        n = sum(1 for ln in lines if ln.startswith(tid + " ") and "：" in ln)
        title = parts[1] if len(parts) > 1 else h
        note = f"：{parts[2]}" if len(parts) > 2 else ""
        out.append(f"{tid} {title}（{n} 项）{note}")
    return "\n".join(out)


async def get_task(args: Args) -> str:
    """取一张任务单的完整条目：任务头 + 全部条目行。"""
    task_id = str(args.get("task_id", "")).strip()
    lines = _read_lines(_TASKS)
    own = [ln for ln in lines if ln.split(" ", 1)[0] == task_id]
    if not own:
        ids = "、".join(sorted({ln.split(" ", 1)[0] for ln in lines if "：" not in ln}))
        return f"未找到任务单：{task_id}。现有：{ids}"
    return "\n".join(own)


# ---------------------------------------------------------------- 审批判量


def _current_stock(category: str) -> int:
    """读 data/inventory.txt 里某品类的当前库存（当场能判，微秒级）。"""
    for ln in _read_lines(_INVENTORY):
        name, _, rest = ln.partition("：")
        if name.strip() == category:
            m = re.search(r"库存\s*(\d+)", rest)
            return int(m.group(1)) if m else 0
    return 0


def _restock_approval(args: Args) -> str | None:
    """判量不判工具名：小补货放行，大额补货问人。

    和 rules.txt 的补货规则同一条线（超 50 件报备）。判不了的参数按
    fail-closed 处理——"问不问人"这件事上，宁可停下来。
    """
    try:
        target = int(args.get("stock", 0))
    except (TypeError, ValueError):
        return "stock 参数无法解析为整数，需人工确认"
    delta = target - _current_stock(str(args.get("category", "")).strip())
    if delta > 50:
        return f"补货 {delta} 件超过 50 件上限，需店长审批"
    return None


@dataclass(frozen=True)
class Tool:
    """一个工具的完整定义：给模型看的 schema、真实现、判量审批声明。

    审批要求声明在工具自己身上（和事件类声明消费约束同一个道理）：
    总线与订阅者不参与审批，agent 执行前查一次声明。
    """

    schema: dict[str, Any]  # function schema：name / description / parameters
    fn: ToolFn
    approval_check: Callable[[Args], str | None] | None = None  # 返回理由 = 要问人


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
        approval_check=_restock_approval,  # 判量：超 50 件的大额补货才问人
    ),
    "search_rules": Tool(
        schema=_fn_schema(
            "search_rules",
            "检索团队规则、流程、制度（如补货、盘点差异、报销）",
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
    "list_tasks": Tool(
        schema=_fn_schema(
            "list_tasks",
            "列出今天的运营任务单（补货核查、盘点差异），带任务号和条目数",
            {},  # 无参数：任务单只读、没有状态字段——进度住在会话里
            [],
        ),
        fn=list_tasks,
    ),
    "get_task": Tool(
        schema=_fn_schema(
            "get_task",
            "取一张任务单的完整条目：品类、目标库存或盘点实物数",
            {"task_id": {"type": "string", "description": "任务号，如 T-101"}},
            ["task_id"],
        ),
        fn=get_task,
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
        "遇到任务单时，逐项处理：先取单，再逐条目查证、按规则处理，"
        "被拒绝或挂起的条目如实汇报，不要重复尝试。"
    )
    return "\n".join(lines)
