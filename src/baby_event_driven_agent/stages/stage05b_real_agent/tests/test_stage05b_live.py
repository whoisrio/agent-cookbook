"""Stage 5b 真模型用例：打 .env 配的模型与嵌入端点（本地 ollama 也能跑）。

只留三条，全部走真实 CLI UI（pipe input 驱动同一条 prompt 循环——
测试打的就是 demo 里那个界面，不是绕过 UI 直接调 agent）：

1. 同义改写查询端到端命中（嵌入端点不可用则如实跳过——降级是机制，不是失败）；
2. agent 回答引用报备规则出处（search_rules 的检索结果是回答的依据）；
3. 写回闭环：一句话记制度 → update_rules → 检索立刻命中新规则。

注意：真模型用例有固有波动（模型可能多查一次、措辞不同），断言只钉
机制事实（工具调用发生过、检索结果带出处、新规则可检索），措辞类断言
放宽到关键词。

前置：仓库根 .env 配好 OPENAI_API_BASE / OPENAI_MODEL，嵌入端点配
EMBEDDING_API_BASE（或与 OPENAI_API_BASE 同栈）+ EMBEDDING_MODEL。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from baby_event_driven_agent.stages.stage05b_real_agent import knowledge as kb
from baby_event_driven_agent.stages.stage05b_real_agent import tools as tools_mod
from baby_event_driven_agent.stages.stage05b_real_agent.tools import RealLLM
from baby_event_driven_agent.stages.stage05b_real_agent.ui import (
    ChatUI,
    restore_data,
    use_data_copies,
)

TIMEOUT = 420.0


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage05b_live_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def kb_copies(workdir: Path) -> tuple[Path, Path]:
    """知识路径换工作目录副本；结束还原（写操作不碰包自带 data/）。"""
    saved = use_data_copies(workdir)
    yield tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH
    restore_data(saved)


@pytest.fixture()
def real_llm() -> Any:  # type: ignore[name-defined]
    try:
        return RealLLM()
    except RuntimeError as exc:
        pytest.skip(f"未配置真模型（.env），跳过：{exc}")


def feed(text: str) -> str:
    return text + "\r"


def test_live_paraphrase_hit(workdir: Path, kb_copies: tuple[Path, Path]) -> None:
    """同义改写查询端到端命中：问法用"垫付"，知识库里只有"交通报销"。"""
    docs_dir, index_path = kb_copies
    index = kb.KnowledgeIndex(docs_dir, index_path, kb.get_embedder())

    async def go() -> None:
        await index.build()

    asyncio.run(asyncio.wait_for(go(), TIMEOUT))
    result = asyncio.run(asyncio.wait_for(index.search("同事垫付的打车钱怎么报销"), TIMEOUT))
    if result.mode != "semantic":
        pytest.skip(f"嵌入端点不可用，走了词面路（mode={result.mode}）——语义路用例跳过")
    assert result.hits, "同义改写应命中"
    assert result.hits[0].chunk.doc == "报销流程"
    assert "来源：报销流程" in kb.format_result(result)


def test_live_agent_cites_via_real_ui(
    workdir: Path, kb_copies: tuple[Path, Path], real_llm: Any
) -> None:
    """agent 回答引用报备规则出处：search_rules 的结果是回答的依据。

    真模型有固有波动（小模型偶尔不查直接答 / 空回复），重试一次再断言。
    """
    searches: list[Any] = []
    transcript = ""
    for attempt in range(3):
        sub = workdir / f"attempt-{attempt}"
        with create_pipe_input() as pipe:
            ui = ChatUI(sub, real_llm, input=pipe, output=DummyOutput())

            async def go() -> None:
                await ui.ensure_index()
                pipe.send_text(feed("马克杯要补 80 件能直接补吗") + feed("/quit"))
                await asyncio.wait_for(ui.chat_loop(), TIMEOUT)
                await ui.close()

            asyncio.run(asyncio.wait_for(go(), TIMEOUT + 60.0))
        transcript = ui.transcript()
        searches = [
            e
            for e in ui.events
            if e.type == "tool_result" and e.payload.get("name") == "search_rules"
        ]
        if searches:
            break
    assert searches, "agent 应调用 search_rules 查报备线"
    result_text = str(searches[0].payload.get("result", ""))
    assert "来源：" in result_text, "检索结果应带出处"
    # 回答要落在检索到的依据上（模型改写的查询词不同，命中的块会漂移，
    # 措辞类断言放宽到"有依据"这个机制事实）
    assert any(k in transcript for k in ("报备", "审批", "补货制度", "流程")), (
        "最终回答应引用检索到的制度依据"
    )


def test_live_write_back_via_real_ui(
    workdir: Path, kb_copies: tuple[Path, Path], real_llm: Any
) -> None:
    """写回闭环：一句话记制度 → update_rules → 检索立刻命中新规则。

    真模型有固有波动（小模型偶尔空回复 / 不调工具），重试一次再断言。
    """
    kb_copies  # 包自带 data/ 的隔离由 ChatUI 自换路径保证；fixture 校验还原
    docs_dir = index_path = None
    writes: list[Any] = []
    for attempt in range(3):
        sub = workdir / f"attempt-{attempt}"
        with create_pipe_input() as pipe:
            ui = ChatUI(sub, real_llm, input=pipe, output=DummyOutput())
            docs_dir, index_path = tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH

            async def go() -> None:
                await ui.ensure_index()
                pipe.send_text(feed("把'盘点差异挂起需店长和财务双签'记进制度") + feed("/quit"))
                await asyncio.wait_for(ui.chat_loop(), TIMEOUT)
                await ui.close()

            asyncio.run(asyncio.wait_for(go(), TIMEOUT + 60.0))
        writes = [
            e
            for e in ui.events
            if e.type == "tool_result" and e.payload.get("name") == "update_rules"
        ]
        if writes:
            break
    assert writes, "agent 应调用 update_rules 记制度"
    assert docs_dir is not None and index_path is not None
    # 文档真写进工作目录副本（close 之后路径已还原，故用抓下来的路径）
    docs_with_rule = [p for p in docs_dir.glob("*.md") if "双签" in p.read_text(encoding="utf-8")]
    assert docs_with_rule, "知识库文档应落盘"
    # 立即可检索
    index = kb.KnowledgeIndex(docs_dir, index_path, None)
    assert index.load()
    result = asyncio.run(index.search("盘点差异挂起需谁审批签字"))
    assert any("双签" in h.chunk.body for h in result.hits)
    assert "知识库文档" in ui.transcript()
