"""知识层（05b 的增量）：文档 → 切块 → 索引 → 检索双路。

文档是源（data/knowledge/*.md，`# 文档标题` + `## 节标题` 组织），索引是
物化产物（JSON 落盘）：可全量重建、可按文档增量更新（重切该文档的块、
原地替换）。与"history 是 log 的投影"同一个形状——派生物永远可重算，
源只有一个；换嵌入模型也是重建，不迁移。

切块：按 `##` 标题切节；超过长度上限的节按空行段落续切（首块带
`文档标题 §节标题：` 前缀，续块只含段落）。每块带来源元数据
（源文件 / 文档标题 / 节标题）——检索结果能指回出处。块 id 由内容派生
（sha256），同一批文档两次构建块 id 完全一致——确定性用离线测试钉死，
这是"检索命中可判定"的前提。

检索双路：
- 语义路（默认）：块向量 × 查询向量 cosine（含词面小权重加权，hybrid），
  OpenAI 兼容 /v1/embeddings
  （配置同 RealLLM：仓库根 .env；嵌入模型独立配置 EMBEDDING_MODEL，
  默认 bge-m3）；
- 词面路（兜底）：字符二元组 + IDF 加权重叠。未配嵌入端点、构建期或
  检索期嵌入调用失败 → 自动降级。降级不是静默的：结果如实标注
  mode（semantic / lexical），诚实纪律沿袭 05 的 fail-open。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from dotenv import dotenv_values

# 节的长度上限（字符）：超过按空行段落续切。教学库的块都不大，
# 上限只挡"整节塞一个块"的极端情况。
MAX_CHUNK_CHARS = 200

DEFAULT_TOP_K = 5
INDEX_VERSION = 1

# 配置读仓库根 .env，环境变量优先——与 stage04 的 RealLLM 同一份纪律
_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONFIG = dotenv_values(_REPO_ROOT / ".env")


def _cfg(key: str) -> str:
    """环境变量优先于 .env 文件——临时换嵌入端点不用改文件。"""
    return os.environ.get(key) or _CONFIG.get(key) or ""


# ---------------------------------------------------------------- 嵌入客户端


class Embedder:
    """OpenAI 兼容 /v1/embeddings 客户端：一个端点 + 一个模型名。"""

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=30.0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(model=self.model, input=texts)
        return [list(item.embedding) for item in resp.data]


def get_embedder() -> Embedder | None:
    """按配置取嵌入客户端；没配嵌入端点返回 None（检索走词面路）。

    端点优先 EMBEDDING_API_BASE，回落 OPENAI_API_BASE（同一本地栈的
    情况下不用重复配）；模型默认 bge-m3，本地 ollama 已验证可用。
    """
    base = _cfg("EMBEDDING_API_BASE") or _cfg("OPENAI_API_BASE")
    if not base:
        return None
    key = _cfg("EMBEDDING_API_KEY") or _cfg("OPENAI_API_KEY") or "EMPTY"
    return Embedder(base_url=base, api_key=key, model=_cfg("EMBEDDING_MODEL") or "bge-m3")


# ---------------------------------------------------------------- 切块


@dataclass(frozen=True)
class Chunk:
    """一个知识块：给模型看的文本 + 指回出处的元数据 + 两路检索用的字段。"""

    id: str
    text: str  # 块内文本（首块：文档标题 §节标题：正文；续块：只含段落）
    body: str  # 正文裸文本（检索结果里返回给模型的部分）
    source: str  # 源文件名
    doc: str  # 文档标题
    section: str  # 节标题
    bigrams: dict[str, int]  # 词面倒排（字符二元组 → 出现次数）
    vector: tuple[float, ...] | None = None  # 嵌入向量（构建期尽力而为）


def _bigrams(text: str) -> dict[str, int]:
    """字符二元组倒排：先去掉全部空白——中文词面匹配不该被换行/空格打断。"""
    flat = re.sub(r"\s+", "", text)
    grams: dict[str, int] = {}
    for i in range(len(flat) - 1):
        gram = flat[i : i + 2]
        grams[gram] = grams.get(gram, 0) + 1
    return grams


def _chunk_id(source: str, doc: str, section: str, seq: int, text: str) -> str:
    """内容派生的块 id：同一批文档两次构建完全一致（确定性测试钉死）。"""
    raw = f"{source}\x00{doc}\x00{section}\x00{seq}\x00{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _split_sections(raw: str) -> tuple[str, list[tuple[str, str]]]:
    """`# 文档标题` + `## 节标题` 组织 → (文档标题, [(节标题, 节正文), ...])。

    `#` 只认第一行（文档标题）；`###` 及更深的标题按普通正文处理。
    标题行之前的段落算前言，节标题记为文档标题本身。
    """
    doc_title = ""
    sections: list[tuple[str, str]] = []
    cur_title = ""
    cur: list[str] = []

    def flush() -> None:
        text = "\n".join(cur).strip()
        if text:
            sections.append((cur_title or doc_title or "正文", text))

    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("# ") and not doc_title:
            doc_title = s[2:].strip()
            continue
        if s.startswith("## "):
            flush()
            cur_title = s[3:].strip()
            cur = []
            continue
        cur.append(ln)
    flush()
    return doc_title or "正文", sections


def chunk_document(source: str, raw: str) -> list[Chunk]:
    """一篇文档 → 有序的块列表（无向量；向量由索引构建阶段补）。

    整节不超长 → 一个块（带 `文档标题 §节标题：` 前缀）；超长 → 首块
    带前缀装第一段，其余段落各自成块（续块只含段落，前缀不重复占位）。
    段落是原子的：单段超长也保持一个块，不截断。
    """
    doc_title, sections = _split_sections(raw)
    chunks: list[Chunk] = []
    seq = 0
    for sec_title, sec_text in sections:
        paragraphs = [
            re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n[ \t]*\n", sec_text)
        ]
        paragraphs = [p for p in paragraphs if p]
        if not paragraphs:
            continue
        prefix = f"{doc_title} §{sec_title}："
        joined = "\n".join(paragraphs)
        if len(prefix) + len(joined) <= MAX_CHUNK_CHARS:
            pieces = [(prefix + joined, joined)]
        else:
            pieces = [(prefix + paragraphs[0], paragraphs[0])]
            pieces.extend((p, p) for p in paragraphs[1:])
        for text, body in pieces:
            chunks.append(
                Chunk(
                    id=_chunk_id(source, doc_title, sec_title, seq, text),
                    text=text,
                    body=body,
                    source=source,
                    doc=doc_title,
                    section=sec_title,
                    bigrams=_bigrams(text),
                )
            )
            seq += 1
    return chunks


# ---------------------------------------------------------------- 索引


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class SearchResult:
    mode: str  # "semantic" | "lexical"——降级不是静默的，如实标注
    hits: list[SearchHit]


def format_result(result: SearchResult) -> str:
    """检索结果 → 给模型看的文本：模式如实标注，每块带出处与分数。"""
    if not result.hits:
        return "（无命中）"
    mode_label = "语义检索" if result.mode == "semantic" else "词面检索（语义路不可用，兜底）"
    lines = [f"检索模式：{mode_label}"]
    for i, hit in enumerate(result.hits, 1):
        lines.append(
            f"[{i}] (来源：{hit.chunk.doc} §{hit.chunk.section}，score {hit.score:.2f}) "
            f"{hit.chunk.body}"
        )
    return "\n".join(lines)


def _cosine(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _chunk_to_json(c: Chunk) -> dict[str, Any]:
    return {
        "id": c.id,
        "text": c.text,
        "body": c.body,
        "source": c.source,
        "doc": c.doc,
        "section": c.section,
        "bigrams": c.bigrams,
        "vector": list(c.vector) if c.vector is not None else None,
    }


def _chunk_from_json(d: dict[str, Any]) -> Chunk:
    vec = d.get("vector")
    return Chunk(
        id=str(d["id"]),
        text=str(d["text"]),
        body=str(d.get("body") or d["text"]),
        source=str(d["source"]),
        doc=str(d["doc"]),
        section=str(d["section"]),
        bigrams={str(k): int(v) for k, v in d.get("bigrams", {}).items()},
        vector=tuple(vec) if vec else None,
    )


class KnowledgeIndex:
    """块的物化产物：JSON 落盘，文档是源，索引永远可重建。

    embedder 注入而非内部创建——离线测试传 None（纯词面路），
    真端点传 Embedder（构建 / 增量时尽力嵌入，失败自动降级词面路）。
    """

    def __init__(self, docs_dir: Path, index_path: Path, embedder: Embedder | None = None) -> None:
        self.docs_dir = docs_dir
        self.index_path = index_path
        self._embedder = embedder
        self._chunks: list[Chunk] = []
        self._embedding_model: str | None = None
        self._loaded = False

    # ------------------------------------------------------------ 读

    def load(self) -> bool:
        """从盘上读索引；文件不存在或损坏返回 False（调用方决定重建）。"""
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._chunks = [_chunk_from_json(c) for c in data["chunks"]]
            self._embedding_model = data.get("embedding_model")
            self._loaded = True
            return True
        except (OSError, KeyError, TypeError, ValueError):
            return False

    def chunks(self) -> list[Chunk]:
        return list(self._chunks)

    # ------------------------------------------------------------ 写路径

    def _save(self) -> None:
        payload = {
            "version": INDEX_VERSION,
            "embedding_model": self._embedding_model,
            "chunks": [_chunk_to_json(c) for c in self._chunks],
        }
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1),
            encoding="utf-8",
        )

    async def _try_embed(self, chunks: list[Chunk]) -> list[Chunk]:
        """尽力给块补向量；嵌入失败不炸构建——索引照建，检索走词面路。"""
        if self._embedder is None or not chunks:
            return chunks
        try:
            vectors = await self._embedder.embed([c.text for c in chunks])
        except Exception:  # noqa: BLE001 - 嵌入端点不可用：降级，不挡构建
            return chunks
        return [replace(c, vector=tuple(v)) for c, v in zip(chunks, vectors)]

    async def build(self) -> None:
        """全量重建：读 docs 目录全部文档 → 切块 → 尽力嵌入 → 落盘。"""
        chunks: list[Chunk] = []
        for path in sorted(self.docs_dir.glob("*.md")):
            chunks.extend(chunk_document(path.name, path.read_text(encoding="utf-8")))
        chunks = await self._try_embed(chunks)
        self._chunks = chunks
        self._embedding_model = self._embedder.model if any(c.vector for c in chunks) else None
        self._loaded = True
        self._save()

    async def ensure(self) -> None:
        """有索引用索引，没有就全量重建（首次检索 / 首次写前的准备步骤）。"""
        if self._loaded or self.load():
            return
        await self.build()

    async def update_document(self, source: str, raw: str) -> None:
        """按文档增量更新：重切该文档的块、原地替换、立即落盘可检索。

        两种情况不增量、直接全量重建：索引还不存在（源文档已在盘上，
        build 自然包含新文档）；嵌入模型与建索引时不同——换嵌入模型
        也是重建，不迁移（向量空间不同，增量混嵌是错的）。
        """
        if not self._loaded and not self.load():
            await self.build()
            return
        if self._embedder is not None and self._embedding_model != self._embedder.model:
            await self.build()
            return
        new_chunks = await self._try_embed(chunk_document(source, raw))
        # 增量嵌入失败而旧块有向量：新块暂时走不了语义路——保持词面路可
        # 检索（mode 如实标注），下次全量重建补齐向量。
        self._chunks = [c for c in self._chunks if c.source != source] + new_chunks
        if all(c.vector is None for c in self._chunks):
            self._embedding_model = None
        self._save()

    # ------------------------------------------------------------ 检索

    async def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> SearchResult:
        """双路检索：语义路优先（嵌入调用失败降级词面路），mode 如实标注。

        语义路内部做词面加权（hybrid）：最终分 = 0.7×cosine + 0.3×词面
        （词面分归一到本库最大值）。纯 cosine 对"查询里带着块里的硬词"
        不敏感——问"马克杯补 80 件"时含"报备线"的块可能被泛补货块挤出
        top-k；词面小权重把这类硬命中拉回来，同义改写仍由 cosine 主导。
        """
        query = query.strip()
        if not query or not self._chunks:
            return SearchResult(mode="lexical", hits=[])
        top_k = max(1, min(int(top_k), len(self._chunks)))
        lex_all = {h.chunk.id: h.score for h in self._lexical(query, len(self._chunks))}
        lex_max = max(lex_all.values()) if lex_all else 0.0
        if (
            self._embedder is not None
            and self._embedding_model == self._embedder.model
            and any(c.vector is not None for c in self._chunks)
        ):
            try:
                qvec = (await self._embedder.embed([query]))[0]
                fused = [
                    SearchHit(
                        c,
                        0.7 * _cosine(qvec, c.vector)
                        + 0.3 * (lex_all.get(c.id, 0.0) / lex_max if lex_max > 0 else 0.0),
                    )
                    for c in self._chunks
                    if c.vector is not None
                ]
                hits = [h for h in fused if h.score > 0.0]
                hits.sort(key=lambda h: (-h.score, h.chunk.id))
                return SearchResult(mode="semantic", hits=hits[:top_k])
            except Exception:  # noqa: BLE001 - 检索期嵌入失败：降级词面路
                pass
        return SearchResult(mode="lexical", hits=self._lexical(query, top_k))

    def _lexical(self, query: str, top_k: int) -> list[SearchHit]:
        """词面路：字符二元组 + IDF 加权重叠（覆盖了多少查询词面质量）。"""
        qgrams = list(_bigrams(query))
        if not qgrams:
            return []
        n = len(self._chunks)
        df: dict[str, int] = {}
        for c in self._chunks:
            for g in c.bigrams:
                df[g] = df.get(g, 0) + 1
        unseen = math.log(1 + n)  # 查询里有、库里没有的 gram：给最大权重
        idf = {g: math.log(1 + n / d) for g, d in df.items()}
        denom = sum(idf.get(g, unseen) for g in qgrams)
        hits: list[SearchHit] = []
        if denom <= 0.0:
            return []
        for c in self._chunks:
            num = sum(idf.get(g, unseen) for g in qgrams if g in c.bigrams)
            if num > 0.0:
                hits.append(SearchHit(c, num / denom))
        hits.sort(key=lambda h: (-h.score, h.chunk.id))
        return hits[:top_k]
