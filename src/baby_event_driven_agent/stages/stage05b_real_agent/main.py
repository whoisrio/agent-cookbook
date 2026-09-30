"""Stage 5b 真知识库演示（[project.scripts] 入口：stage05b-demo）。

与 05b 章 demo 对应。agent / 总线 / 轨迹层继续共享 04 的实现，本包只落
知识层与工具后端。四段 + 交互模式：

  01-build-index      构建管道：列文档、切块、节标题；连续两次构建块 id
                      逐字节一致（确定性看得见）；索引 JSON 落盘
  02-search-upgrade   同义改写对照：旧逐行匹配无命中，知识库命中报销流程
                      块并给出出处与分数；mode（semantic / lexical）如实标注
  03-agent-with-kb    真模型：问"马克杯要补 80 件能直接补吗"——agent 调
                      search_rules 拿到报备线与审批链，回答带依据；
                      超线补货触发审批，无人确认按拒绝，如实汇报
  04-write-back       真模型："把'盘点差异挂起需店长和财务双签'记进制度"
                      → update_rules 重写文档 → 索引增量更新 → 检索命中新规则
  chat                交互模式：真实 stdin 的对话 UI（不脚本化）

01 / 02 离线可跑；03 / 04 真模型（读仓库根 .env，本地 ollama 也行），
.env 缺失自动跳过。03 / 04 与真模型测试走同一条真 UI（pipe input 驱动
prompt 循环，渲染即所见）——demo 与测试只有输入来源不同。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from baby_event_driven_agent.stages.stage04_trajectory import tools as base_tools
from baby_event_driven_agent.stages.stage05b_real_agent import knowledge as kb
from baby_event_driven_agent.stages.stage05b_real_agent import tools as tools_mod
from baby_event_driven_agent.stages.stage05b_real_agent.tools import RealLLM
from baby_event_driven_agent.stages.stage05b_real_agent.ui import (
    BOLD,
    BLUE,
    GREEN,
    GREY,
    ORANGE,
    RED,
    RESET,
    YELLOW,
    ChatUI,
    line,
    restore_data,
    use_data_copies,
)


def banner(name: str, title: str, what: str) -> None:
    print(f"\n{BOLD}── {name} · {title} ──{RESET}")
    print(f"{GREY}       说明 │ {what}{RESET}")


def note(text: str) -> None:
    print(f"{GREY}       说明 │ {text}{RESET}")


def feed(text: str) -> str:
    """pipe input 的一行：prompt_toolkit 以 \\r 为回车。"""
    return text + "\r"


def _swap(workdir: Path) -> dict[str, Path]:
    return use_data_copies(workdir)


def _fresh_index() -> kb.KnowledgeIndex:
    """按当前（可能已被换到工作目录的）路径新建索引对象。"""
    return kb.KnowledgeIndex(
        tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH, kb.get_embedder()
    )


# ---------------------------------------------------------------- demo 1：构建管道


async def case_build_index(workdir: Path) -> None:
    banner(
        "01-build-index",
        "构建管道：文档 → 切块 → 索引（离线，不依赖模型）",
        "运营手册是多篇 markdown（源），索引是物化产物（JSON）。看三样："
        "切块的节标题与元数据、连续两次构建块 id 逐字节一致、索引文件落盘。",
    )
    saved = _swap(workdir)
    try:
        docs = sorted(tools_mod._KNOWLEDGE_DIR.glob("*.md"))
        print(f"{BLUE}源文档（{len(docs)} 篇）{RESET}")
        total = 0
        for path in docs:
            chunks = kb.chunk_document(path.name, path.read_text(encoding="utf-8"))
            total += len(chunks)
            sections = "、".join(dict.fromkeys(c.section for c in chunks))
            print(f"{GREY}  {path.name}：{len(chunks)} 块 ［{sections}］{RESET}")
        line("切块", BLUE, f"共 {total} 块；每块带来源（源文件 / 文档标题 / 节标题）")

        first = _fresh_index()
        await first.build()
        ids_first = [c.id for c in first.chunks()]
        again = _fresh_index()
        await again.build()
        ids_second = [c.id for c in again.chunks()]
        line(
            "确定性",
            GREEN if ids_first == ids_second else RED,
            f"两次构建块 id 完全一致：{ids_first == ids_second}"
            "（id 由内容派生，检索命中因此可判定）",
        )

        reloaded = _fresh_index()
        loaded = reloaded.load()
        with_vectors = sum(1 for c in reloaded.chunks() if c.vector is not None)
        model_line = (
            f"，嵌入模型 {reloaded._embedding_model}"
            if reloaded._embedding_model
            else "（嵌入端点不可用，索引照建，检索走词面路）"
        )
        line(
            "索引",
            GREEN if loaded else RED,
            f"{tools_mod._INDEX_PATH.name} 落盘：{len(reloaded.chunks())} 块，"
            f"带向量 {with_vectors} 块{model_line}",
        )
        note("文档是源，索引永远可重建——换嵌入模型也是重建，不迁移。")
    finally:
        restore_data(saved)


# ---------------------------------------------------------------- demo 2：检索升级


async def case_search_upgrade(workdir: Path) -> None:
    banner(
        "02-search-upgrade",
        "同义改写对照：逐行匹配 vs 知识库检索（离线可跑，真嵌入端点则走语义路）",
        "用户问\"同事垫付的打车钱怎么报销\"——规则行里没有\"垫付\"两个字，"
        "词面匹配全盲；知识库按 top-k 命中报销流程块并给出出处与分数。",
    )
    saved = _swap(workdir)
    try:
        query = "同事垫付的打车钱怎么报销"

        old = await base_tools.search_rules({"query": query})
        line("旧（04 逐行匹配）", YELLOW, old)

        index = _fresh_index()
        await index.ensure()
        result = await index.search(query)
        line(
            "新（知识库 top-k）",
            GREEN if result.hits else RED,
            kb.format_result(result).replace("\n", "\n       │ "),
        )
        assert result.hits, "同义改写应命中报销流程块"
        assert result.hits[0].chunk.doc == "报销流程", (
            f"应命中报销流程，实际 {result.hits[0].chunk.doc}"
        )
        print(f"{GREY}       说明 │ 检索模式 mode={result.mode}，降级不是静默的{RESET}")

        keyword = "补货超过 50 件要报备吗"
        old_kw = await base_tools.search_rules({"query": keyword})
        new_kw = await index.search(keyword)
        top = new_kw.hits[0]
        line(
            "对照（关键词问法）",
            BLUE,
            f"旧：{old_kw.splitlines()[0][:60]}… ／ 新命中《{top.chunk.doc} "
            f"§{top.chunk.section}》——原来能查到的现在一样能查到",
        )
        note("工具名和调用方式模型侧完全不变，换的是工具背后的世界。")
    finally:
        restore_data(saved)


# ---------------------------------------------------------------- 真 UI 脚本段


async def run_scripted(workdir: Path, lines: list[str]) -> ChatUI:
    """pipe input 驱动真 UI 跑一段脚本对话——与真模型测试同一条路。

    返回未关闭的 UI（调用方看完 events 后自行 close）：验证要用工作目录
    里的索引与文档，路径在 close 恢复之前才指向工作目录。
    """
    with create_pipe_input() as pipe:
        ui = ChatUI(workdir, input=pipe, output=DummyOutput())
        await ui.ensure_index()
        pipe.send_text("".join(feed(x) for x in lines) + feed("/quit"))
        await asyncio.wait_for(ui.chat_loop(), ui.timeout)
    return ui


def tool_results(ui: ChatUI, name: str) -> list[str]:
    return [
        str(e.payload.get("result", ""))
        for e in ui.events
        if e.type == "tool_result" and e.payload.get("name") == name
    ]


# ---------------------------------------------------------------- demo 3：带知识库的 agent


async def case_agent_with_kb(workdir: Path) -> None:
    banner(
        "03-agent-with-kb",
        "带真知识库的 agent（真模型，走真实 CLI UI）",
        "问\"马克杯要补 80 件能直接补吗\"——agent 调 search_rules 拿到报备线"
        "与审批链，回答带依据；超线补货触发审批，无人确认按拒绝，如实汇报。",
    )
    try:
        RealLLM()  # .env 缺失时在这里跳过，不进真模型段
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return

    ui = await run_scripted(workdir, ["马克杯要补 80 件能直接补吗"])
    # 真模型偶发空回复 / 不调工具（小模型波动）：重试一次
    if not tool_results(ui, "search_rules"):
        print(f"{GREY}       （模型没调检索工具，波动重试一次）{RESET}")
        ui = await run_scripted(workdir, ["马克杯要补 80 件能直接补吗"])

    searches = tool_results(ui, "search_rules")
    inventory = tool_results(ui, "query_inventory")
    approvals = [e for e in ui.events if e.type == "approval_required"]
    line(
        "工具轨迹",
        BLUE,
        f"search_rules × {len(searches)}、query_inventory × {len(inventory)}、"
        f"审批请求 × {len(approvals)}",
    )
    if searches:
        line(
            "检索依据",
            GREEN if "来源：" in searches[0] else RED,
            searches[0].replace("\n", "\n       │ "),
        )
    if approvals:
        line("审批", ORANGE, f"{approvals[0].payload.get('reason')}（无人确认 → 按拒绝处理）")
    await ui.close()
    note("回答带出处（补货制度 §报备线），超线补货走审批——依据来自知识库，不是模型编的。")


# ---------------------------------------------------------------- demo 4：写回闭环


async def case_write_back(workdir: Path) -> None:
    banner(
        "04-write-back",
        "知识运营闭环（真模型，走真实 CLI UI）",
        "\"把'盘点差异挂起需店长和财务双签'记进制度\" → update_rules 重写整篇"
        "文档（设值语义）→ 重切块、原地更新索引 → 立即可检索。",
    )
    try:
        RealLLM()
    except RuntimeError as exc:
        line("系统", ORANGE, f"跳过真模型段：{exc}")
        return

    index = _fresh_index()
    await index.ensure()
    before_total = len(index.chunks())

    ui = await run_scripted(workdir, ["把'盘点差异挂起需店长和财务双签'记进制度"])

    writes = [str(e.payload.get("result", "")) for e in ui.events if e.type == "tool_result"]
    writes = [w for w in writes if "知识库文档" in w]
    line("写入", GREEN if writes else RED, writes[0] if writes else "（模型未调用 update_rules）")
    # close 会把模块路径还原回包内：工作目录路径要先抓下来
    kb_dir, index_path = tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH
    await ui.close()

    # 验证按实际落盘的文档来（标题由模型给，不硬编码）：含新规则的文档 + 索引可检索
    index = kb.KnowledgeIndex(kb_dir, index_path, kb.get_embedder())
    index.load()
    after = index.chunks()
    written = sorted({c.source for c in after if "双签" in c.body})
    has_new = bool(written)
    line(
        "索引增量",
        GREEN if has_new else RED,
        f"全库 {before_total} 块 → {len(after)} 块；"
        f"落盘文档：{'、'.join(written) if written else '（未找到）'}；"
        f"新规则已可检索：{has_new}（整篇重写，设值语义）",
    )

    result = await index.search("盘点差异挂起需谁审批签字")
    hit_texts = " ".join(h.chunk.body for h in result.hits)
    line(
        "检索命中",
        GREEN if "双签" in hit_texts else RED,
        kb.format_result(result).replace("\n", "\n       │ "),
    )
    note("写入 → 重切 → 更新索引 → 可检索，一个 turn 内完成——知识运营有了闭环。")


# ---------------------------------------------------------------- 入口

CASE_ORDER = ("01-build-index", "02-search-upgrade", "03-agent-with-kb", "04-write-back")
CASE_TITLES = {
    "01-build-index": "构建管道：两次构建块 id 一致，索引落盘",
    "02-search-upgrade": "同义改写对照：逐行匹配无命中，知识库带出处命中",
    "03-agent-with-kb": "带真知识库的 agent：回答带依据，超线补货走审批",
    "04-write-back": "知识运营闭环：写入 → 索引更新 → 立即可检索",
}
CASES = {
    "01-build-index": case_build_index,
    "02-search-upgrade": case_search_upgrade,
    "03-agent-with-kb": case_agent_with_kb,
    "04-write-back": case_write_back,
}


async def main(case_ids: list[str] | None = None, sessions_dir: Path | None = None) -> None:
    root = sessions_dir or (Path(__file__).resolve().parents[2] / "sessions" / "stage05b")
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    if case_ids and "chat" in case_ids:
        ui = ChatUI(root / "chat")
        try:
            await ui.ensure_index()
            await ui.chat_loop()
        finally:
            await ui.close()
        return

    picked = [c for c in CASE_ORDER if not case_ids or "all" in case_ids or c in case_ids]
    if not picked:
        print(f"{GREY}没有匹配的 case：{case_ids}；可选：all, chat, {', '.join(CASE_ORDER)}{RESET}")
        return

    print(f"{BOLD}Stage 5b 真知识库演示 —— 与 05b 章 demo 对应{RESET}")
    if case_ids and "all" not in case_ids:
        print(f"{GREY}  （只跑：{', '.join(picked)}）{RESET}")

    for i, cid in enumerate(picked):
        workdir = root / cid.split("-", 1)[1]
        workdir.mkdir(parents=True, exist_ok=True)
        await CASES[cid](workdir)
        if i < len(picked) - 1:
            print()

    print(f"\n{BOLD}── 收尾 ──{RESET}")
    line("系统", ORANGE, "demo 结束")
    print(f"{GREY}  session log: {root}{RESET}")
    note("交互模式：stage05b-demo chat")


def cli() -> None:
    """[project.scripts] 入口：stage05b-demo。

        stage05b-demo                     # 四段全跑（01/02 离线，03/04 真模型）
        stage05b-demo 02-search-upgrade   # 只跑指定段
        stage05b-demo chat                # 交互模式：真实 stdin 的对话 UI
        stage05b-demo --list              # 列 case 及其说明
    """
    parser = argparse.ArgumentParser(
        prog="stage05b-demo", description="Stage 5b 真知识库演示（与 05b 章 demo 对应）。"
    )
    parser.add_argument("cases", nargs="*", metavar="CASE", help="要跑的 case（默认全部）")
    parser.add_argument("--list", action="store_true", help="列出所有 case 后退出")
    parser.add_argument("--sessions-dir", default=None, help="轨迹落点")
    args = parser.parse_args()
    if args.list:
        print("all\t四段全跑")
        for cid in CASE_ORDER:
            print(f"{cid}\t{CASE_TITLES[cid]}")
        print("chat\t交互模式：真实 stdin 的对话 UI")
        return
    asyncio.run(
        main(args.cases or None, Path(args.sessions_dir) if args.sessions_dir else None)
    )


if __name__ == "__main__":
    cli()
