"""RAG 测试：分词、BM25、向量召回、混合排序、片段切分。

BM25 的测试不只测"能返回结果"，而是测**打分公式的实现是正确的**：
用两个文档构造"某个词只在一篇里出现"的场景，断言 IDF 让稀有词胜出；
用"长度相同/不同"的文档断言 b 参数确实在起作用。
这样才能说明实现了 BM25，而不是"某种关键词匹配"。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.rag.bm25 import BM25Index, ConstrainedBM25
from app.rag.loader import Document, load_documents, split_sections
from app.rag.retriever import Retriever, build_chunks, get_ready_retriever
from app.rag.splitter import normalize, tokenize, tokenize_query
from app.rag.vector_store import (
    HashEmbedder,
    VectorIndex,
    cosine_topk,
    hash_embed,
    min_max_normalize,
)


# =============================================================================
# 分词
# =============================================================================
def test_tokenize_chinese_bigrams() -> None:
    tokens = tokenize("续借规则")
    assert "续借" in tokens
    assert "借规" in tokens
    assert "规则" in tokens


def test_tokenize_keeps_ascii_words_whole() -> None:
    tokens = tokenize("BM25 和 spring-boot 3.12")
    assert "bm25" in tokens
    assert "spring-boot" in tokens
    assert "3.12" in tokens


def test_tokenize_filters_stopword_bigrams() -> None:
    assert "的了" not in tokenize("我的了的书")


def test_tokenize_query_adds_whole_phrase() -> None:
    tokens = tokenize_query("索书号")
    assert "索书号" in tokens  # 整词保留，提升术语精确匹配


def test_normalize_fullwidth_to_halfwidth() -> None:
    assert normalize("ＡＢＣ１２３") == "abc123"


def test_tokenize_empty_text() -> None:
    assert tokenize("") == []


# =============================================================================
# BM25 数学正确性
# =============================================================================
def test_bm25_ranks_matching_document_first() -> None:
    index = BM25Index()
    index.build(
        [
            ("d1", "本科生最多可借 10 册，借期 30 天"),
            ("d2", "逾期罚金 0.2 元每天，封顶为书价"),
            ("d3", "图书馆开放时间 08:00 到 22:00"),
        ]
    )
    hits = index.search("逾期罚金", top_k=3)
    assert hits, "应该召回结果"
    assert hits[0].chunk_id == "d2"


def test_bm25_idf_is_positive_and_decreases_with_df() -> None:
    """IDF 随文档频率上升而下降——这是 BM25 能压制"到处都是的词"的原因。"""
    index = BM25Index()
    index.build(
        [
            ("d1", "借书 规则 说明"),
            ("d2", "借书 规则 补充"),
            ("d3", "借书 规则 常见问题"),
            ("d4", "预约 排队 规则"),
        ]
    )
    rare = index.idf("预约")
    common = index.idf("借书")
    assert rare > common > 0
    # 平滑后 IDF 恒为正（df == N 时也不会变负）
    assert index.idf("借书") > 0


def test_bm25_tf_saturation() -> None:
    """k1 的作用：词频翻倍带来的增益必须远小于线性（饱和）。"""
    index = BM25Index(k1=1.2, b=0.75)
    index.build([("d1", "罚金"), ("d2", "罚金 罚金 罚金 罚金 罚金 罚金")])
    scores = index.score("罚金")
    # 6 次出现相对 1 次的增益必须远小于 6 倍（若不饱和会接近 6 倍）
    ratio = scores["d2"] / scores["d1"]
    assert 1.0 < ratio < 3.0


def test_bm25_length_normalization_with_b() -> None:
    """b 的作用：长文档命中同样次数时得分更低（长度归一化）。"""
    index = BM25Index(k1=1.2, b=0.75)
    index.build(
        [
            ("short", "预约规则"),
            ("long", "预约规则 " + "无关内容 " * 30),
        ]
    )
    scores = index.score("预约规则")
    assert scores["short"] > scores["long"]

    no_norm = BM25Index(k1=1.2, b=0.0)
    no_norm.build(
        [
            ("short", "预约规则"),
            ("long", "预约规则 " + "无关内容 " * 30),
        ]
    )
    flat = no_norm.score("预约规则")
    # b=0 时长度不影响，两篇同样命中的文档得分应接近相等
    assert math.isclose(flat["short"], flat["long"], rel_tol=1e-6)


def test_bm25_empty_index_returns_nothing() -> None:
    index = BM25Index()
    index.build([])
    assert index.search("任何") == []
    assert index.size == 0


def test_bm25_query_with_no_known_terms() -> None:
    index = BM25Index()
    index.build([("d1", "借阅规则")])
    assert index.search("zzzyyy") == []


def test_bm25_returns_top_k_in_descending_order() -> None:
    index = BM25Index()
    index.build(
        [
            ("d1", "罚金 罚金 罚金 逾期"),
            ("d2", "罚金 逾期"),
            ("d3", "罚金"),
            ("d4", "完全无关"),
        ]
    )
    hits = index.search("罚金 逾期", top_k=2)
    assert len(hits) == 2
    assert hits[0].score >= hits[1].score


def test_constrained_bm25_downweights_no_overlap() -> None:
    docs = {
        "a": "本科生最多可以借 10 册书",
        "b": "研究生最多可以借 15 册书",
    }
    index = BM25Index()
    index.build(list(docs.items()))
    constrained = ConstrainedBM25(index, docs)
    hits = constrained.search("研究生能借几本", top_k=2)
    assert hits[0].chunk_id == "b"


# =============================================================================
# 向量召回
# =============================================================================
def test_hash_embed_is_deterministic() -> None:
    a = hash_embed("逾期罚金 0.2 元")
    b = hash_embed("逾期罚金 0.2 元")
    assert np.array_equal(a, b)


def test_hash_embed_is_normalized() -> None:
    vec = hash_embed("借阅规则与罚金")
    assert np.isclose(float(np.linalg.norm(vec)), 1.0, atol=1e-5)


def test_hash_embed_empty_text_is_zero_vector() -> None:
    vec = hash_embed("")
    assert float(np.linalg.norm(vec)) == 0.0


def test_cosine_topk_orders_by_similarity() -> None:
    matrix = np.array(
        [
            [1.0, 0.0],
            [0.0, 1.0],
            [0.7071, 0.7071],
        ],
        dtype=np.float32,
    )
    query = np.array([1.0, 0.0], dtype=np.float32)
    hits = cosine_topk(query, matrix, top_k=3)
    assert hits[0][0] == 0
    assert hits[0][1] > hits[1][1]


def test_cosine_topk_handles_zero_query() -> None:
    matrix = np.ones((2, 4), dtype=np.float32)
    assert cosine_topk(np.zeros(4, dtype=np.float32), matrix, top_k=2) == []


@pytest.mark.asyncio
async def test_vector_index_search_returns_relevant_chunk() -> None:
    index = VectorIndex(HashEmbedder())
    await index.build(
        [
            ("d1", "本科生可借 10 册，借期 30 天"),
            ("d2", "逾期罚金 0.2 元每天"),
            ("d3", "图书馆开放时间 08:00 到 22:00"),
        ]
    )
    hits = await index.search("罚金 逾期", top_k=2)
    assert hits
    assert hits[0].chunk_id == "d2"


def test_min_max_normalize() -> None:
    assert min_max_normalize({}) == {}
    assert min_max_normalize({"a": 5.0}) == {"a": 1.0}
    result = min_max_normalize({"a": 0.0, "b": 10.0})
    assert result == {"a": 0.0, "b": 1.0}
    # 全同分：都归一为 1，把决策权交给另一路召回
    assert min_max_normalize({"a": 3.0, "b": 3.0}) == {"a": 1.0, "b": 1.0}


# =============================================================================
# 切分
# =============================================================================
def test_split_sections_builds_breadcrumb() -> None:
    doc = Document(
        doc_id="t",
        title="借阅规则",
        text="# 借阅规则\n\n## 逾期与罚金\n\n罚金 0.2 元每天。\n\n## 预约\n\n到馆保留 3 天。\n",
        source="knowledge/t.md",
    )
    sections = split_sections(doc)
    assert len(sections) == 2
    assert sections[0].breadcrumb == "借阅规则 > 逾期与罚金"
    assert "0.2" in sections[0].text
    assert sections[1].breadcrumb == "借阅规则 > 预约"


def test_split_sections_handles_deep_headings() -> None:
    doc = Document(
        doc_id="t",
        title="总览",
        text="# 总览\n\n## 一级\n\n### 二级\n\n内容\n\n## 另一个一级\n\n内容2\n",
        source="knowledge/t.md",
    )
    sections = split_sections(doc)
    assert sections[0].breadcrumb == "总览 > 一级 > 二级"
    # 同级标题出现后必须砍掉旧路径，不能拼成"总览 > 一级 > 二级 > 另一个一级"
    assert sections[1].breadcrumb == "总览 > 另一个一级"


def test_build_chunks_adds_breadcrumb_prefix() -> None:
    doc = Document(
        doc_id="t", title="规则", text="# 规则\n\n## 罚金\n\n0.2 元每天。\n", source="knowledge/t.md"
    )
    chunks = build_chunks([doc], chunk_size=320, chunk_overlap=60)
    assert len(chunks) == 1
    assert chunks[0].text.startswith("【规则 > 罚金】")


def test_build_chunks_splits_long_section_with_overlap() -> None:
    body = "。".join(f"第{i}条规则内容" for i in range(1, 60)) + "。"
    doc = Document(doc_id="t", title="长文档", text=f"# 长文档\n\n## 大节\n\n{body}\n", source="k/t.md")
    chunks = build_chunks([doc], chunk_size=120, chunk_overlap=30)
    assert len(chunks) > 1
    assert all(len(c.text) <= 200 for c in chunks)


def test_load_documents_reads_knowledge_base() -> None:
    from app.config import get_settings

    docs = load_documents(get_settings().knowledge_path)
    assert len(docs) >= 9, "知识库至少要有 9 篇文档"
    titles = {d.doc_id for d in docs}
    assert any("借阅规则" in t for t in titles)
    assert all(d.text.strip() for d in docs)


# =============================================================================
# 检索器（端到端）
# =============================================================================
@pytest.mark.asyncio
async def test_retriever_builds_from_real_knowledge_base(test_settings) -> None:
    retriever = Retriever(test_settings)
    await retriever.build()
    stats = retriever.stats
    assert stats["chunks"] > 20
    assert stats["docs"] >= 9
    assert stats["bm25_k1"] == 1.2
    assert stats["bm25_b"] == 0.75
    assert stats["vector_enabled"] is True


@pytest.mark.parametrize(
    "query,expected_substring",
    [
        ("本科生能借几本", "10"),
        ("研究生借期多久", "60"),
        ("教师能借多少册", "20"),
        ("逾期罚金怎么算", "0.2"),
        ("预约到馆保留几天", "3 天"),
        ("博士能借几本", ""),  # 知识库里没有博士，必须不硬凑
        ("计算机类的书在几楼", "3 楼"),
        ("图书馆几点开门", "08:00"),
    ],
)
@pytest.mark.asyncio
async def test_retriever_finds_expected_facts(test_settings, query, expected_substring) -> None:
    retriever = await get_ready_retriever(test_settings)
    hits = await retriever.retrieve(query, top_k=3)
    assert hits, f"{query} 应该有召回结果"
    if expected_substring:
        joined = "\n".join(h.chunk.text for h in hits)
        assert expected_substring in joined, f"{query} 的召回里没有 {expected_substring}"


@pytest.mark.asyncio
async def test_retriever_dedupes_same_heading(test_settings) -> None:
    retriever = await get_ready_retriever(test_settings)
    hits = await retriever.retrieve("借阅规则", top_k=5)
    keys = [(h.chunk.doc_id, h.chunk.heading) for h in hits]
    assert len(keys) == len(set(keys)), "同一标题的多个窗口不应重复出现"


@pytest.mark.asyncio
async def test_retriever_hybrid_marks_match_source(test_settings) -> None:
    retriever = await get_ready_retriever(test_settings)
    hits = await retriever.retrieve("逾期罚金", top_k=3)
    assert any("bm25" in h.matched_by for h in hits)
    # 两路分数都要如实记录，便于解释"为什么排这个位置"
    assert all(h.bm25_score >= 0 and h.vector_score >= 0 for h in hits)


@pytest.mark.asyncio
async def test_retriever_vector_disabled_still_works(test_settings) -> None:
    limited = test_settings.model_copy(update={"rag_enable_vector": False})
    retriever = Retriever(limited)
    await retriever.build()
    hits = await retriever.retrieve("预约保留", top_k=3)
    assert hits
    assert all(h.matched_by == ["bm25"] for h in hits)


@pytest.mark.asyncio
async def test_retriever_scoped_to_doc(test_settings) -> None:
    retriever = await get_ready_retriever(test_settings)
    hits = await retriever.retrieve("罚金", top_k=3, doc_id="03-逾期与罚金")
    assert hits
    assert all(h.chunk.doc_id == "03-逾期与罚金" for h in hits)


@pytest.mark.asyncio
async def test_retriever_empty_query(test_settings) -> None:
    retriever = await get_ready_retriever(test_settings)
    assert await retriever.retrieve("   ", top_k=3) == []


@pytest.mark.asyncio
async def test_answer_hint_returns_first_meaningful_line(test_settings) -> None:
    retriever = await get_ready_retriever(test_settings)
    hint = retriever.answer_hint("罚金标准")
    assert hint
    assert "【" not in hint  # 不返回面包屑

    assert retriever.answer_hint("zzzqqq") is None
