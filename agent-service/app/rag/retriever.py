"""混合检索：BM25 + 可选向量召回 → 分数归一化 → 加权融合 → 去重。

为什么是"混合"而不是二选一：
- BM25 强在**精确词形匹配**：读者问"续借几次"，能精准命中含"续借"的规则条目；
- 向量召回强在**语义相近**：读者问"书快到期了还能再延期吗"，
  字面没有"续借"，但语义就是续借；哈希嵌入抓不到，真实 embedding 能抓到。
两者互补，所以默认 0.7/0.3 加权（BM25 更重，因为它是可解释、可控的那一路，
且在我们的短规则文本上准确率更高）。

融合方式选"归一化后线性加权"而不是 RRF：
RRF 只看排名、丢弃分差，在只有几百个 chunk 的知识库上会把"其实只有一条相关"
和"有三条都相关"的情况拉平；线性加权保留了分差信息，更适合小规模精排。
（如果要扩展到万级语料，RNN/RRF 更稳，这一点写进 README 的局限性说明。）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence
from app.config import Settings, get_settings
from app.rag.bm25 import BM25Index
from app.rag.loader import Document, load_documents, split_sections
from app.rag.splitter import cjk_trigrams, tokenize, tokenize_query
from app.rag.vector_store import (
    BaseEmbedder,
    FallbackEmbedder,
    HashEmbedder,
    OpenAICompatEmbedder,
    VectorIndex,
    min_max_normalize,
)

#: 句子切分：中英文句末标点 + 换行。用于超长 section 的窗口切分
_SENTENCE_RE = re.compile(r"[^。！？；\n]+[。！？；\n]?", re.MULTILINE)


@dataclass
class Chunk:
    """检索的最小单元：一个 Markdown 标题单元（过长则再切窗口）。"""

    chunk_id: str
    text: str
    doc_id: str
    doc_title: str
    heading: str
    breadcrumb: str
    source: str
    line_no: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "doc_title": self.doc_title,
            "heading": self.heading,
            "breadcrumb": self.breadcrumb,
            "source": self.source,
            "line_no": self.line_no,
        }


@dataclass
class Retrieved:
    """一条召回结果（含两路分数，便于解释"为什么排在这个位置"）。"""

    chunk: Chunk
    score: float
    bm25_score: float = 0.0
    vector_score: float = 0.0
    matched_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk.chunk_id,
            "text": self.chunk.text,
            "source": self.chunk.source,
            "breadcrumb": self.chunk.breadcrumb,
            "score": round(self.score, 4),
            "bm25_score": round(self.bm25_score, 4),
            "vector_score": round(self.vector_score, 4),
            "matched_by": self.matched_by,
        }


def _windows(text: str, size: int, overlap: int) -> list[str]:
    """按句子边界做带重叠的窗口切分，避免把一句话劈成两半。"""
    sentences = [s for s in _SENTENCE_RE.findall(text) if s.strip()]
    if not sentences:
        return [text] if text.strip() else []
    out: list[str] = []
    buf: list[str] = []
    length = 0
    for sentence in sentences:
        if length + len(sentence) > size and buf:
            out.append("".join(buf).strip())
            # 重叠：保留尾部若干句子，保证跨窗口的语义连续
            keep: list[str] = []
            keep_len = 0
            for s in reversed(buf):
                if keep_len + len(s) > overlap:
                    break
                keep.insert(0, s)
                keep_len += len(s)
            buf = keep
            length = keep_len
        buf.append(sentence)
        length += len(sentence)
    if buf:
        out.append("".join(buf).strip())
    return [w for w in out if w]


def build_chunks(docs: Sequence[Document], *, chunk_size: int, chunk_overlap: int) -> list[Chunk]:
    """Document → Section → Chunk。

    每个 chunk 的文本都会前缀 breadcrumb（"借阅规则 > 逾期与罚金"），
    这样即使 chunk 被单独送进 prompt，也有足够的上下文，
    并且 breadcrumb 里的词会进倒排索引，提升定位精度。
    """
    chunks: list[Chunk] = []
    for doc in docs:
        for sec_idx, section in enumerate(split_sections(doc), start=1):
            body = section.text.strip()
            if not body:
                continue
            header = f"【{section.breadcrumb}】"
            parts = (
                [body]
                if len(body) <= chunk_size
                else _windows(body, chunk_size, chunk_overlap)
            )
            for win_idx, part in enumerate(parts, start=1):
                chunk_id = (
                    f"{doc.doc_id}#{sec_idx}"
                    if len(parts) == 1
                    else f"{doc.doc_id}#{sec_idx}.{win_idx}"
                )
                chunks.append(
                    Chunk(
                        chunk_id=chunk_id,
                        text=f"{header}\n{part}".strip(),
                        doc_id=doc.doc_id,
                        doc_title=doc.title,
                        heading=section.heading,
                        breadcrumb=section.breadcrumb,
                        source=section.source,
                        line_no=section.line_no,
                    )
                )
    return chunks


class Retriever:
    """知识库混合检索器。进程内单例（见 get_retriever）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        embedder: BaseEmbedder | None = None,
        chunks: Sequence[Chunk] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.chunks: list[Chunk] = list(chunks or [])
        self.by_id: dict[str, Chunk] = {c.chunk_id: c for c in self.chunks}
        self.bm25 = BM25Index()
        self.vector = VectorIndex(embedder or self._default_embedder())
        self._built = False

    def _default_embedder(self) -> BaseEmbedder:
        """按配置决定是否接真实 embedding；默认哈希嵌入（零依赖）。"""
        cfg = self.settings
        if cfg.rag_enable_vector and cfg.llm_provider == "openai_compat" and cfg.llm_api_key:
            primary = OpenAICompatEmbedder(cfg.llm_base_url, cfg.llm_api_key)
            return FallbackEmbedder(primary, HashEmbedder())
        return HashEmbedder()

    # ---------------- 构建 ----------------
    async def build(self, knowledge_dir: str | None = None) -> "Retriever":
        """加载知识库并建两路索引。幂等：可重复调用（评测/测试里会重建）。"""
        path = knowledge_dir or self.settings.knowledge_path
        docs = load_documents(path)
        self.chunks = build_chunks(
            docs,
            chunk_size=self.settings.rag_chunk_size,
            chunk_overlap=self.settings.rag_chunk_overlap,
        )
        self.by_id = {c.chunk_id: c for c in self.chunks}
        pairs = [(c.chunk_id, c.text) for c in self.chunks]
        self.bm25.build(pairs)
        if self.settings.rag_enable_vector:
            await self.vector.build(pairs)
        self._built = True
        return self

    @property
    def ready(self) -> bool:
        return self._built and bool(self.chunks)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "chunks": len(self.chunks),
            "docs": len({c.doc_id for c in self.chunks}),
            "bm25_vocab": self.bm25.vocab_size,
            "vector_size": self.vector.size,
            "vector_dim": self.vector.dim,
            "embedder": getattr(self.vector.embedder, "active_name", self.vector.embedder.name),
            "vector_enabled": self.settings.rag_enable_vector,
            "bm25_k1": self.bm25.k1,
            "bm25_b": self.bm25.b,
        }

    # ---------------- 检索 ----------------
    async def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        doc_id: str | None = None,
        use_vector: bool | None = None,
    ) -> list[Retrieved]:
        """混合召回。

        doc_id 用于把检索范围限制在某一篇知识文档内（policy_qa 的 topic 路由会用）。
        """
        if not self._built:
            await self.build()
        k = top_k or self.settings.rag_top_k
        if not query.strip() or not self.chunks:
            return []

        # 多取一些候选再做融合，避免 BM25 的第 4 名被向量第 1 名挤掉时丢失
        pool = max(k * 3, 8)
        bm25_hits = self.bm25.search(query, top_k=pool)
        bm25_raw = {h.chunk_id: h.score for h in bm25_hits}

        vector_raw: dict[str, float] = {}
        want_vector = self.settings.rag_enable_vector if use_vector is None else use_vector
        if want_vector:
            for hit in await self.vector.search(query, top_k=pool):
                vector_raw[hit.chunk_id] = hit.score

        bm25_norm = min_max_normalize(bm25_raw)
        vector_norm = min_max_normalize(vector_raw)
        w_bm25 = self.settings.rag_bm25_weight
        w_vec = self.settings.rag_vector_weight if vector_raw else 0.0
        total_w = (w_bm25 if bm25_norm else 0.0) + w_vec
        if total_w <= 0:
            return []

        if doc_id:
            bm25_norm = {
                cid: s
                for cid, s in bm25_norm.items()
                if cid in self.by_id and self.by_id[cid].doc_id == doc_id
            }
            vector_norm = {
                cid: s
                for cid, s in vector_norm.items()
                if cid in self.by_id and self.by_id[cid].doc_id == doc_id
            }

        fused: dict[str, Retrieved] = {}
        for cid in set(bm25_norm) | set(vector_norm):
            chunk = self.by_id.get(cid)
            if chunk is None:
                continue
            score = (
                w_bm25 * bm25_norm.get(cid, 0.0) + w_vec * vector_norm.get(cid, 0.0)
            ) / total_w
            matched = []
            if cid in bm25_norm:
                matched.append("bm25")
            if cid in vector_norm:
                matched.append("vector")
            fused[cid] = Retrieved(
                chunk=chunk,
                score=score,
                bm25_score=bm25_raw.get(cid, 0.0),
                vector_score=vector_raw.get(cid, 0.0),
                matched_by=matched,
            )

        ranked = sorted(fused.values(), key=lambda r: (-r.score, r.chunk.chunk_id))
        return self._dedupe(ranked)[:k]

    @staticmethod
    def _dedupe(items: list[Retrieved]) -> list[Retrieved]:
        """去重：同一 (doc, heading) 只保留最高分的一条。

        为什么需要：超长 section 被切成多个窗口后，同一标题会产生多个 chunk，
        它们内容高度重叠，全塞给 LLM 既浪费 token 又会让同一句话被重复强调。
        """
        seen: set[tuple[str, str]] = set()
        out: list[Retrieved] = []
        for item in items:
            key = (item.chunk.doc_id, item.chunk.heading)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def answer_hint(self, query: str) -> str | None:
        """给"规则库直答"用的确定性短答：命中最相关 chunk 的第一句有效内容。

        用途是 policy_qa 工具的**降级路径**：当 Java 后端的 /policy 规则库不可用时，
        仍然能返回一条有出处（source）的答案，而不是空手而归。
        """
        hits = self.bm25.search(query, top_k=1)
        if not hits:
            return None
        chunk = self.by_id.get(hits[0].chunk_id)
        if chunk is None:
            return None
        for line in chunk.text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("【") or stripped.startswith("#"):
                continue
            if stripped.startswith(("-", "*", "|")):
                # 表格/列表行也作为答案（知识库大量使用表格列规则）
                return stripped.lstrip("-*| ").strip()
            return stripped
        return None

    # ---------------- 相关性判定（给 policy_qa 用的可信度闸门）----------------
    def relevance_weight(self, query: str, text: str, *, top_terms: int = 3) -> dict[str, Any]:
        """衡量"这条片段到底答的是不是这个查询"。

        主判据是 **trigram 覆盖率**（`tri_coverage`）：
        "查询里的三元组有多少真的出现在片段里"。这是 Lucene 处理中文的常规思路，
        比 bigram 可靠得多——bigram 会把"预约到馆保留几天"切成"约保""留几"
        这类根本不存在的组合，命中与否都说明不了问题。

        辅以 IDF 加权覆盖率（`ratio`）与高 IDF 主题词命中数（`top_hit`），
        三个指标一起看，避免单一阈值被长句/短句带偏。
        真正定位主题的词（如"天文/文台"）不出现时，覆盖率会明显偏低。
        """
        terms = {t for t in tokenize_query(query) if len(t) >= 2}
        if not terms or not self.bm25.size:
            return {"ratio": 1.0, "top_hit": 1, "tri_coverage": 1.0, "top_terms": [], "hits": []}

        # tokenize_query 会额外塞入"整段中文短语"用于精确匹配，但这些短语在索引里
        # 基本不存在，IDF 会被算成最高分——如果拿它们排"主题词"，会得出
        # "整句本身才是主题词"这种废话（实测：导致所有长问句 top_hit 都是 0）。
        # 因此 IDF 加权只统计 bigram / 英文词这类**索引侧真实存在的词元**。
        index_terms = {t for t in tokenize(query)}
        idf = {t: self.bm25.idf(t) for t in terms if t in index_terms}
        if not idf:
            return {"ratio": 1.0, "top_hit": 1, "tri_coverage": 1.0, "top_terms": [], "hits": []}

        query_idf = sum(idf.values()) or 0.0
        doc_terms = set(tokenize(text))
        hits = [t for t in idf if t in doc_terms]
        covered_idf = sum(idf[t] for t in hits)
        ranked = sorted(idf, key=lambda t: -idf[t])[:top_terms]
        top_hit = sum(1 for t in ranked if t in doc_terms)

        query_tris = cjk_trigrams(query)
        doc_tris = cjk_trigrams(text)
        tri_coverage = (
            len(query_tris & doc_tris) / len(query_tris) if query_tris else 1.0
        )
        return {
            "covered_idf": covered_idf,
            "query_idf": query_idf,
            "ratio": (covered_idf / query_idf) if query_idf > 0 else 1.0,
            "top_hit": top_hit,
            "tri_coverage": tri_coverage,
            "top_terms": ranked,
            "hits": hits,
        }

    def distinctive_terms(self, query: str, *, max_df_ratio: float = 0.35) -> set[str]:
        """挑出查询里"有区分度"的词元（df 不超过总 chunk 数的 35%）。"""
        terms = {t for t in tokenize_query(query) if len(t) >= 2}
        if not terms or not self.bm25.size:
            return terms
        cutoff = max(1.0, self.bm25.size * max_df_ratio)
        return {t for t in terms if self.bm25.document_frequency(t) <= cutoff}

    def distinctive_terms(self, query: str, *, max_df_ratio: float = 0.35) -> set[str]:
        """挑出查询里"有区分度"的词元（df 不超过总 chunk 数的 35%）。"""
        terms = {t for t in tokenize_query(query) if len(t) >= 2}
        if not terms or not self.bm25.size:
            return terms
        cutoff = max(1.0, self.bm25.size * max_df_ratio)
        return {t for t in terms if self.bm25.document_frequency(t) <= cutoff}


_RETRIEVER: Retriever | None = None


def get_retriever(settings: Settings | None = None, *, force_rebuild: bool = False) -> Retriever:
    """进程级单例。

    建索引要读文件 + 分词 + 算嵌入，一次对话里可能被调多次（policy_qa + 规则兜底），
    重复建索引会让首 Token 延迟从毫秒级劣化到百毫秒级。
    """
    global _RETRIEVER
    if _RETRIEVER is None or force_rebuild:
        _RETRIEVER = Retriever(settings)
    return _RETRIEVER


async def get_ready_retriever(settings: Settings | None = None) -> Retriever:
    retriever = get_retriever(settings)
    if not retriever.ready:
        await retriever.build()
    return retriever


__all__ = ["Chunk", "Retrieved", "Retriever", "build_chunks", "get_retriever", "get_ready_retriever"]
