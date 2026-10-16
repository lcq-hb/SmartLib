"""自研 BM25（标准库实现，不依赖 rank_bm25）。

为什么自研：
蓝图 §3.2 把"自研 BM25 + 分词"列为 Agent 服务的选型，因为面试里最能说明问题的
不是"我调了库"，而是"我知道 BM25 的 k1 和 b 各自控制什么、为什么要这么取"。

公式（Robertson & Zaragoza 的经典形式）：
    IDF(q) = ln( 1 + (N - df + 0.5) / (df + 0.5) )          ← 加 1 平滑，恒为正
    score(D, Q) = Σ IDF(q) · (tf · (k1 + 1)) / (tf + k1 · (1 - b + b · |D| / avgdl))

参数取值（沿用 Elasticsearch 默认，是有依据的工程惯例）：
- k1 = 1.2：控制词频饱和速度。取大会让"重复出现的词"无限加分，
  取小会削弱关键词密度的影响。1.2 在短文本（我们的 chunk 只有 1~3 段）上表现稳。
- b = 0.75：控制长度归一化强度。b=1 完全归一化（长文档被过度惩罚），
  b=0 不归一化（长 chunk 靠"字多"占便宜）。0.75 是折中。

实现要点：
- 倒排表 postings[term][chunk_idx] = tf，避免每个查询都全量扫描所有 chunk；
- 预计算 avgdl 与每个 chunk 的长度，保证查询路径只做加法与乘法；
- 中英文都用 (1-b+b·len/avgdl) 归一，长度按词元数算而不是字符数。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.rag.splitter import tokenize, tokenize_query

#: Elasticsearch / Lucene 默认值，见模块 docstring 的取舍说明
DEFAULT_K1 = 1.2
DEFAULT_B = 0.75


@dataclass
class BM25Hit:
    chunk_id: str
    score: float


class BM25Index:
    """对一组 (chunk_id, text) 建倒排索引并提供 BM25 打分。"""

    def __init__(self, k1: float = DEFAULT_K1, b: float = DEFAULT_B) -> None:
        self.k1 = k1
        self.b = b
        self.chunk_ids: list[str] = []
        self.doc_len: list[int] = []
        self.avgdl: float = 0.0
        self._postings: dict[str, dict[int, int]] = defaultdict(dict)
        self._df: dict[str, int] = {}
        self._idf_cache: dict[str, float] = {}
        self._built = False

    # ---------------- 建索引 ----------------
    def build(self, docs: Sequence[tuple[str, str]]) -> "BM25Index":
        """docs 为 (chunk_id, text) 序列。重复 build 会整体重建（幂等）。"""
        self.chunk_ids = [cid for cid, _ in docs]
        self.doc_len = []
        self._postings = defaultdict(dict)
        self._df = {}
        self._idf_cache = {}

        for idx, (_, text) in enumerate(docs):
            # 索引侧含 breadcrumb（标题路径已在 chunk.text 里），提升上下文区分度
            tokens = tokenize(text)
            self.doc_len.append(len(tokens) or 1)
            for term, freq in Counter(tokens).items():
                self._postings[term][idx] = freq
                self._df[term] = self._df.get(term, 0) + 1

        total = len(docs)
        self.avgdl = (sum(self.doc_len) / total) if total else 0.0
        self._built = True
        return self

    # ---------------- 打分 ----------------
    @property
    def size(self) -> int:
        return len(self.chunk_ids)

    @property
    def vocab_size(self) -> int:
        return len(self._df)

    def idf(self, term: str) -> float:
        """BM25 的 IDF。缓存是因为同一次检索里同一 term 会被反复查。"""
        cached = self._idf_cache.get(term)
        if cached is not None:
            return cached
        n = self.size
        df = self._df.get(term, 0)
        value = math.log(1.0 + (n - df + 0.5) / (df + 0.5)) if n else 0.0
        self._idf_cache[term] = value
        return value

    def document_frequency(self, term: str) -> int:
        """包含该词元的文档数。

        暴露它是为了支持"区分性词"判定（见 retriever.distinctive_terms）：
        高频词（df 接近 N）没有定位能力，不该用来判断召回是否切题。
        """
        return self._df.get(term, 0)

    def search(self, query: str, top_k: int = 5, *, min_score: float = 0.0) -> list[BM25Hit]:
        """返回按 BM25 分数降序的 top_k 结果。

        只在命中词元对应的倒排链上累加分数 —— 这是倒排索引的意义所在，
        复杂度 O(命中文档数) 而不是 O(全库文档数)。
        """
        if not self._built or not self.chunk_ids:
            return []
        scores: dict[int, float] = defaultdict(float)
        for term, qf in Counter(tokenize_query(query)).items():
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = self.idf(term)
            if idf <= 0:
                continue
            # 查询词重复出现时按 log 衰减加权，避免把一句话里的同一个词算成多次强信号
            q_weight = 1.0 + math.log(qf) if qf > 1 else 1.0
            for idx, tf in postings.items():
                dl = self.doc_len[idx]
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
                scores[idx] += idf * q_weight * (tf * (self.k1 + 1)) / (denom or 1.0)

        hits = [
            BM25Hit(chunk_id=self.chunk_ids[idx], score=score)
            for idx, score in scores.items()
            if score > min_score
        ]
        hits.sort(key=lambda h: (-h.score, h.chunk_id))
        return hits[:top_k]

    def score(self, query: str) -> dict[str, float]:
        """返回全部命中 chunk 的分数（评测里做召回率分析用）。"""
        return {h.chunk_id: h.score for h in self.search(query, top_k=self.size or 1)}

    def terms_for(self, text: str) -> list[str]:
        """暴露分词结果，方便测试与调试"为什么没召回"。"""
        return tokenize(text)


class ConstrainedBM25:
    """带"必须命中查询关键词"约束的检索。

    场景：用户问"研究生能借几本"，如果只用 BM25 排序，
    可能把"本科生可借 10 册"排在前面（共享"可借/册"词元）。
    加一条规则：结果必须命中查询里的**核心名词词元**（长度 ≥ 2 的中文 bigram 或英文词），
    否则降权。这是纯 BM25 之上的一层业务约束，不改变 BM25 本身。
    """

    def __init__(self, index: BM25Index, docs: dict[str, str]) -> None:
        self.index = index
        self.docs = docs

    def search(self, query: str, top_k: int = 5) -> list[BM25Hit]:
        hits = self.index.search(query, top_k=max(top_k * 3, top_k))
        query_terms = {t for t in tokenize_query(query) if len(t) >= 2}
        if not query_terms:
            return hits[:top_k]
        rescored: list[BM25Hit] = []
        for hit in hits:
            doc_terms = set(tokenize(self.docs.get(hit.chunk_id, "")))
            overlap = len(query_terms & doc_terms)
            # 命中核心词的比例作为乘数：一个词都没命中的直接压到很低
            factor = 1.0 + 0.3 * overlap if overlap else 0.4
            rescored.append(BM25Hit(chunk_id=hit.chunk_id, score=hit.score * factor))
        rescored.sort(key=lambda h: (-h.score, h.chunk_id))
        return rescored[:top_k]


__all__ = ["BM25Index", "BM25Hit", "ConstrainedBM25", "DEFAULT_K1", "DEFAULT_B"]
