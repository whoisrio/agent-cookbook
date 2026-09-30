"""Stage 5b 离线用例：切块 / 索引确定性 / 双路检索 / 写回闭环 / schema 继承 / 真 UI。

全部不依赖模型与嵌入端点（嵌入 monkeypatch 掉，走词面路或假向量）：
- 切块：`##` 标题切节、超长节按段落续切（续块只含段落）、元数据完整
- 确定性：同一批文档两次构建，索引 JSON 逐字节一致（检索命中可判定的前提）
- 检索：词面路命中预期块、无命中如实返回、嵌入失败自动降级并标注 mode
- 写回：update_rules 写文档 → 重切块 → 原地更新索引 → 立即可检索；设值语义
- 继承：四件套原样（schema / fn / 审批声明同一个对象），search_rules 加 top_k
- 真 UI：pipe input 驱动 chat_loop（与真模型测试同一条路），ScriptedLLM 离线验渲染
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

from baby_event_driven_agent.stages.stage04_trajectory import tools as base_tools
from baby_event_driven_agent.stages.stage05b_real_agent import knowledge as kb
from baby_event_driven_agent.stages.stage05b_real_agent import tools as tools_mod
from baby_event_driven_agent.stages.stage05b_real_agent.tools import (
    TOOL_SCHEMAS,
    TOOLS,
    search_rules,
    update_rules,
)
from baby_event_driven_agent.stages.stage05b_real_agent.ui import ChatUI


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


@pytest.fixture()
def workdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stage05b_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def kb_offline(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> Path:
    """知识路径换工作目录副本 + 嵌入禁用（纯离线词面路）；结束验包内文件没动。"""
    pkg_dir = tools_mod._KNOWLEDGE_DIR
    pkg_index = tools_mod._INDEX_PATH
    before = {p.name: p.read_bytes() for p in pkg_dir.glob("*.md")}
    index_before = pkg_index.read_bytes() if pkg_index.exists() else None
    monkeypatch.setattr(kb, "get_embedder", lambda: None)
    dst = workdir / "knowledge"
    dst.mkdir()
    for name, data in before.items():
        (dst / name).write_bytes(data)
    monkeypatch.setattr(tools_mod, "_KNOWLEDGE_DIR", dst)
    monkeypatch.setattr(tools_mod, "_INDEX_PATH", workdir / "knowledge_index.json")
    yield dst
    assert {p.name: p.read_bytes() for p in pkg_dir.glob("*.md")} == before
    assert (pkg_index.read_bytes() if pkg_index.exists() else None) == index_before


# ---------------------------------------------------------------- 切块


def test_chunk_document_splits_sections() -> None:
    raw = "# 补货制度\n\n## 报备线\n\n超 50 件要先报备店长审批。\n\n## 审批链\n\n店长审批后执行。\n"
    chunks = kb.chunk_document("补货制度.md", raw)
    assert [c.section for c in chunks] == ["报备线", "审批链"]
    assert chunks[0].text == "补货制度 §报备线：超 50 件要先报备店长审批。"
    assert chunks[0].body == "超 50 件要先报备店长审批。"
    assert chunks[0].doc == "补货制度" and chunks[0].source == "补货制度.md"
    assert len({c.id for c in chunks}) == 2  # 内容派生 id，块块不同


def test_long_section_splits_by_paragraph() -> None:
    para_a = "甲" * 120
    para_b = "乙" * 120
    raw = f"# 制度\n\n## 长节\n\n{para_a}\n\n{para_b}\n"
    chunks = kb.chunk_document("制度.md", raw)
    assert len(chunks) == 2
    # 首块带前缀、只装第一段；续块只含段落，前缀不重复占位
    assert chunks[0].text.startswith("制度 §长节：")
    assert chunks[0].text.endswith(para_a) and chunks[0].body == para_a
    assert chunks[1].text == para_b and chunks[1].section == "长节"


def test_whitespace_neutral_bigrams() -> None:
    """词面倒排对空白不敏感：换行/空格不打断中文二元组。"""
    assert kb._bigrams("报销 流程") == kb._bigrams("报销流程")
    assert kb._bigrams("报\n销")["报销"] == 1


# ---------------------------------------------------------------- 确定性


def test_build_deterministic(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text(
        "# 甲\n\n## 一\n\n第一段。\n\n## 二\n\n第二段。\n", encoding="utf-8"
    )
    (docs / "b.md").write_text("# 乙\n\n## 一\n\n乙的段落。\n", encoding="utf-8")
    i1 = kb.KnowledgeIndex(docs, tmp_path / "i1.json", None)
    run(i1.build())
    i2 = kb.KnowledgeIndex(docs, tmp_path / "i2.json", None)
    run(i2.build())
    assert (tmp_path / "i1.json").read_bytes() == (tmp_path / "i2.json").read_bytes()
    assert [c.id for c in i1.chunks()] == [c.id for c in i2.chunks()]


# ---------------------------------------------------------------- 检索双路


def _build_offline(workdir: Path) -> kb.KnowledgeIndex:
    index = kb.KnowledgeIndex(
        tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH, kb.get_embedder()
    )
    run(index.build())
    return index


def test_lexical_search_hits_expected_chunk(kb_offline: Path) -> None:
    index = _build_offline(kb_offline.parent)
    result = run(index.search("打车费怎么报销"))
    assert result.mode == "lexical"
    assert result.hits, "应命中报销流程的交通费块"
    assert result.hits[0].chunk.doc == "报销流程"
    formatted = kb.format_result(result)
    assert "来源：报销流程" in formatted and "score" in formatted


def test_search_no_hits_is_honest(kb_offline: Path) -> None:
    index = _build_offline(kb_offline.parent)
    result = run(index.search("xyzqrs abcd"))
    assert result.hits == []
    assert kb.format_result(result) == "（无命中）"


def test_semantic_path_with_fake_embedder(kb_offline: Path) -> None:
    class FakeEmbedder:
        model = "fake-embed"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] if "报销" in t else [0.0, 1.0] for t in texts]

    index = kb.KnowledgeIndex(
        tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH, FakeEmbedder()
    )
    run(index.build())
    result = run(index.search("报销流程怎么走"))
    assert result.mode == "semantic"
    assert result.hits[0].chunk.doc == "报销流程"


def test_semantic_path_fuses_lexical_boost(kb_offline: Path) -> None:
    """hybrid：向量不区分时，词面硬命中把正确的块拉回 top-1。"""

    class FlatEmbedder:
        """所有文本返回同一个向量：cosine 全相等，排序只能靠词面加权。"""

        model = "flat-embed"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    index = kb.KnowledgeIndex(
        tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH, FlatEmbedder()
    )
    run(index.build())
    result = run(index.search("报备线"))
    assert result.mode == "semantic"
    assert result.hits[0].chunk.doc == "补货制度"
    assert result.hits[0].chunk.section == "报备线"


def test_embed_failure_degrades_to_lexical(kb_offline: Path) -> None:
    class BrokenEmbedder:
        def __init__(self, model: str = "broken") -> None:
            self.model = model

        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("嵌入端点不可用")

    # 构建期失败：索引照建（无向量），检索走词面路
    index = kb.KnowledgeIndex(
        tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH, BrokenEmbedder()
    )
    run(index.build())
    assert all(c.vector is None for c in index.chunks())
    result = run(index.search("打车费怎么报销"))
    assert result.mode == "lexical" and result.hits

    # 检索期失败：有向量的索引同样降级，mode 如实标注
    index2 = kb.KnowledgeIndex(
        tools_mod._KNOWLEDGE_DIR,
        tools_mod._INDEX_PATH,
        BrokenEmbedder(model="fake-embed"),
    )
    run(index2.build())
    index2._embedder = BrokenEmbedder(model="fake-embed")  # 查询时端点坏掉
    result2 = run(index2.search("打车费怎么报销"))
    assert result2.mode == "lexical" and result2.hits


# ---------------------------------------------------------------- 写回闭环


def test_update_rules_writeback_and_set_semantics(kb_offline: Path) -> None:
    out = run(
        update_rules(
            {"title": "盘点差异处理", "content": "盘点差异挂起需店长和财务双签。"}
        )
    )
    assert "已更新" in out and "立即可检索" in out
    # 文档整篇重写（设值语义）
    doc = kb_offline / "盘点差异处理.md"
    assert doc.exists() and "双签" in doc.read_text(encoding="utf-8")
    # 索引原地更新：该文档的旧块全被替换，新块立即可检索
    index = kb.KnowledgeIndex(
        tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH, kb.get_embedder()
    )
    assert index.load()
    own = [c for c in index.chunks() if c.source == "盘点差异处理.md"]
    assert own and all("双签" in c.body for c in own)
    result = run(index.search("盘点差异挂起需谁审批签字"))
    assert any("双签" in h.chunk.body for h in result.hits)

    # 再次写入同标题：重写不叠加
    run(update_rules({"title": "盘点差异处理", "content": "挂起差异复盘三天内出结论。"}))
    index2 = kb.KnowledgeIndex(
        tools_mod._KNOWLEDGE_DIR, tools_mod._INDEX_PATH, kb.get_embedder()
    )
    assert index2.load()
    own2 = [c for c in index2.chunks() if c.source == "盘点差异处理.md"]
    assert all("双签" not in c.body for c in own2)


def test_update_rules_validation(kb_offline: Path) -> None:
    assert "未执行" in run(update_rules({"title": "", "content": "x"}))
    assert "未执行" in run(update_rules({"title": "x", "content": ""}))


def test_search_rules_tool_formats_with_mode_and_top_k(kb_offline: Path) -> None:
    out = run(search_rules({"query": "打车费怎么报销", "top_k": 2}))
    assert "检索模式" in out and "来源：报销流程" in out
    assert out.count("[") <= 3  # 模式行 + 至多两块
    assert "缺少 query" in run(search_rules({}))


# ---------------------------------------------------------------- schema 继承


def test_schemas_inherited_from_stage04() -> None:
    assert set(TOOLS) == set(base_tools.TOOLS)  # 六件套同名同数
    # 四件套原样：schema / 实现 / 审批声明是同一个对象
    for name in ("query_inventory", "update_inventory", "list_tasks", "get_task"):
        assert TOOLS[name].schema == base_tools.TOOLS[name].schema
        assert TOOLS[name].fn is base_tools.TOOLS[name].fn
        assert TOOLS[name].approval_check is base_tools.TOOLS[name].approval_check
    sr = TOOLS["search_rules"].schema["function"]
    assert sr["parameters"]["required"] == ["query"]
    assert set(sr["parameters"]["properties"]) == {"query", "top_k"}
    ur = TOOLS["update_rules"].schema["function"]
    assert set(ur["parameters"]["properties"]) == {"title", "content"}
    assert [s["function"]["name"] for s in TOOL_SCHEMAS] == list(TOOLS)


# ---------------------------------------------------------------- 真 UI（离线）


class ScriptedLLM:
    """脚本化 LLM（同 04 离线用例）：按调用次数吐脚本块，验 UI 渲染路径。

    `delay`：每次流调用前先睡一会儿——把"turn 确定在飞"从竞速变成确定。
    """

    def __init__(self, script: list[list[dict[str, Any]]], delay: float = 0.0) -> None:
        self.script = script
        self.delay = delay
        self.calls = 0

    async def stream_chat(self, messages: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        for chunk in self.script[idx]:
            yield chunk


def test_real_ui_renders_tool_and_reply(kb_offline: Path) -> None:
    """pipe input 驱动真实全屏应用：用户输入 / 思考 / 回答 / 工具执行 / 结果都进消息区。"""
    script = [
        [
            {"type": "reasoning_delta", "text": "用户问报销，先查知识库"},
            {
                "type": "tool_call_delta",
                "index": 0,
                "id": "call_1",
                "name": "search_rules",
                "args_delta": '{"query": "打车费怎么报销"}',
            },
        ],
        [{"type": "text_delta", "text": "报销流程：发起申请 -> 财务审核 -> 打款。"}],
    ]
    with create_pipe_input() as pipe:
        ui = ChatUI(kb_offline.parent, ScriptedLLM(script), input=pipe, output=DummyOutput())

        async def go() -> None:
            await ui.ensure_index()
            pipe.send_text("同事垫付的打车钱怎么报销\r/quit\r")
            await asyncio.wait_for(ui.chat_loop(), 30.0)
            await ui.close()

        run(asyncio.wait_for(go(), 60.0))
    out = ui.transcript()
    assert "你 > 同事垫付的打车钱怎么报销" in out  # 用户输入上屏（黄）
    assert "▏思考 " in out and "先查知识库" in out  # 模型思考上屏（暗灰流式）
    assert "┌─ search_rules" in out and '"query"' in out  # 工具执行：名字 + 参数
    assert "└─ " in out and "来源：报销流程" in out  # 工具结果（青）带出处
    assert "▏回答 " in out  # 模型回复流式前缀
    assert "报销流程：发起申请" in out  # 回复内容上屏
    # 写隔离：UI 关闭后工作目录里的索引在、包内文件没动（kb_offline teardown 复核）
    assert (kb_offline.parent / "knowledge_index.json").exists()


def test_real_ui_supports_steering_and_interrupt(kb_offline: Path) -> None:
    """turn 在飞时输入框不被占用：Enter=转向插话，/stop=打断。

    ScriptedLLM 带延迟：第一问的 turn 确定在飞时，后续输入依次是
    转向（busy=True 的窗口内）→ 打断 → 退出。
    """
    script = [[{"type": "text_delta", "text": "回答的一部分。"}]]
    with create_pipe_input() as pipe:
        ui = ChatUI(
            kb_offline.parent, ScriptedLLM(script, delay=2.0), input=pipe, output=DummyOutput()
        )

        async def go() -> None:
            await ui.ensure_index()
            pipe.send_text("第一问\r")
            await asyncio.sleep(0.5)  # turn 确定在飞（流挂在 2s 延迟上）
            pipe.send_text("插一句转向\r")
            await asyncio.sleep(0.3)
            pipe.send_text("/stop\r")
            await asyncio.sleep(0.5)
            pipe.send_text("/quit\r")
            await asyncio.wait_for(ui.chat_loop(), 30.0)
            await ui.close()

        run(asyncio.wait_for(go(), 60.0))
    out = ui.transcript()
    assert "你 > 第一问" in out
    assert "（转向）你 > 插一句转向" in out  # 转向插话进了当前轮
    assert "已发送打断" in out  # /stop 发了打断
