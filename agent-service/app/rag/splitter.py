"""分词与文本规范化。

为什么不用 jieba 等分词器：
蓝图 §3.2 明确要求"自研 BM25 + 分词，零外部依赖"。
中文检索的经典做法是**字符二元组（bigram）**：不需要词典也能获得很好的召回，
而且对专业术语/新词（"索书号""到馆保留"）天然友好——这正是图书馆检索的痛点。
代价是索引变大（词表量级 O(2n)），在本项目的知识库规模下完全可接受。

英文/数字仍按整词处理，避免 "bm25" 被切成 "bm"/"25" 造成误召回。
"""

from __future__ import annotations

import re

# 连续英文字母/数字（含中间的点与连字符，如 "spring-boot"、"3.12"）
_ASCII_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.\-]*|\d+(?:\.\d+)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

#: 中文停用词：这些词在任何文档里都高频，留着只会稀释 BM25 的区分度
STOPWORDS: frozenset[str] = frozenset(
    {
        "的", "了", "是", "在", "和", "与", "或", "及", "等", "我", "你", "他", "她",
        "这", "那", "有", "没有", "会", "可以", "需要", "如果", "那么", "因为", "所以",
        "一个", "什么", "怎么", "如何", "哪些", "多少", "请", "问", "下", "一下",
        "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "着", "过", "被", "把", "对", "为",
        "从", "到", "就", "都", "也", "还", "又", "很", "更", "最", "不", "没",
    }
)


def normalize(text: str) -> str:
    """统一大小写与全角标点，减少同一句话的不同写法造成的漏召回。"""
    lowered = text.lower()
    # 全角字符 → 半角（含全角数字与字母）
    out_chars: list[str] = []
    for ch in lowered:
        code = ord(ch)
        if code == 0x3000:
            out_chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out_chars.append(chr(code - 0xFEE0))
        else:
            out_chars.append(ch)
    return "".join(out_chars)


def tokenize(text: str) -> list[str]:
    """把文本切成检索词元：中文 bigram + 英文/数字整词。

    >>> tokenize("续借规则 BM25")
    ['续借', '借规', '规则', 'bm25']
    """
    if not text:
        return []
    norm = normalize(text)
    tokens: list[str] = []

    for match in _ASCII_WORD_RE.finditer(norm):
        word = match.group(0).strip(".-_")
        if len(word) >= 2 or word.isdigit():
            tokens.append(word)

    for match in _CJK_RE.finditer(norm):
        run = match.group(0)
        if len(run) == 1:
            # 单字也保留（"书""借"这类单字查询很常见），但不进停用词过滤之外的逻辑
            if run not in STOPWORDS:
                tokens.append(run)
            continue
        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram in STOPWORDS:
                continue
            # 两个字符都在停用词里（如"的了"）直接丢弃
            if run[i] in STOPWORDS and run[i + 1] in STOPWORDS:
                continue
            tokens.append(bigram)

    return tokens


def tokenize_query(text: str) -> list[str]:
    """查询侧分词：额外保留整段中文短语，提升"索书号"这类术语的精确匹配权重。"""
    tokens = tokenize(text)
    for match in _CJK_RE.finditer(normalize(text)):
        run = match.group(0)
        if 2 <= len(run) <= 8:
            tokens.append(run)
    return tokens


def cjk_trigrams(text: str) -> set[str]:
    """抽取中文三元组（trigram）。

    用途不是检索，而是**相关性判定**（见 retriever.trigram_coverage）。
    理由：bigram 太碎（"预约到馆保留几天"会切出"约保""留几"这种不存在的组合），
    而"命中了多少个碎片"无法说明片段是否切题；trigram 在中文里的信息量接近"词"，
    用它算"查询有多少内容真的出现在片段里"最稳。
    这是 Lucene/ES 处理中文的常见做法（CJK bigram/trigram 分析器）。
    """
    if not text:
        return set()
    norm = normalize(text)
    out: set[str] = set()
    for match in _CJK_RE.finditer(norm):
        run = match.group(0)
        for i in range(len(run) - 2):
            tri = run[i : i + 3]
            if all(ch in STOPWORDS for ch in tri):
                continue
            out.add(tri)
    return out


__all__ = ["tokenize", "tokenize_query", "cjk_trigrams", "normalize", "STOPWORDS"]
