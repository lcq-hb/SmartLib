"""向量召回：numpy 余弦相似度 + 无 Key 时的确定性哈希嵌入降级。

为什么要有"哈希嵌入"而不是"没 Key 就不做向量召回"：
蓝图 §3.2 要求"无 Key 时用哈希嵌入降级"。这不是糊弄——
它本质是 **hashing trick + 随机投影**（类似 feature hashing）：
把每个词元哈希到固定维度并带符号累加，得到的是一个"词袋的稠密近似"。
它抓不到真正的语义相似（"还书" <-> "归还"），但能抓到词形重叠，
作为 BM25 的**补充召回**（而不是替代）是有价值的，且完全离线、完全确定性。

Embedding 接口留了 openai_compat 实现：配了 LLM_API_KEY 且 provider 为
openai_compat 时自动启用真实 embedding；任何失败都静默降级回哈希嵌入，
因为"向量召回挂掉"绝不能让整个问答挂掉。
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from typing import Sequence

import httpx

import numpy as np

from app.rag.splitter import tokenize

#: 哈希嵌入维度。取 256 是经验值：知识库只有几百个 chunk，
#: 维度太高会让哈希冲突概率下降的收益被"近乎正交导致余弦噪声"抵消。
HASH_DIM = 256


def _hash_pair(token: str) -> tuple[int, float]:
    """稳定哈希：Python 内置 hash() 受 PYTHONHASHSEED 影响，跨进程不稳定，
    会让"同一份知识库每次启动召回结果不同"，这在评测里是致命的。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = struct.unpack("<Q", digest)[0]
    index = value % HASH_DIM
    sign = 1.0 if (value >> 63) & 1 == 0 else -1.0
    return index, sign


def hash_embed(text: str, dim: int = HASH_DIM) -> np.ndarray:
    """确定性哈希嵌入（L2 归一化，便于直接用点积算余弦）。"""
    vec = np.zeros(dim, dtype=np.float32)
    tokens = tokenize(text)
    if not tokens:
        return vec
    for token in tokens:
        index, sign = _hash_pair(token)
        # 词频用 1+log(tf) 压缩，与 BM25 的词频饱和思路一致
        vec[index] += sign
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec


class BaseEmbedder:
    """嵌入接口。"""

    name = "base"
    dim = HASH_DIM

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        raise NotImplementedError

    async def embed_query(self, text: str) -> np.ndarray:
        return (await self.embed([text]))[0]


class HashEmbedder(BaseEmbedder):
    """默认嵌入：纯本地、确定性、零依赖。"""

    name = "hash"
    dim = HASH_DIM

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack([hash_embed(t, self.dim) for t in texts])


class FallbackEmbedder(BaseEmbedder):
    """优先用真实 embedding，失败或未配置时自动降级到哈希嵌入。

    这是"可选向量召回"的工程落点：调用方不需要知道当前用的是哪种实现，
    只看 `active_name` 就能在 /admin/metrics 里如实展示。
    """

    def __init__(self, primary: BaseEmbedder, fallback: BaseEmbedder | None = None) -> None:
        self.primary = primary
        self.fallback = fallback or HashEmbedder()
        self.degraded = False
        self.last_error: str | None = None
        self.dim = getattr(primary, "dim", HASH_DIM)

    @property
    def name(self) -> str:
        return self.active_name

    @property
    def active_name(self) -> str:
        return f"{self.fallback.name}(fallback:{self.primary.name})" if self.degraded else self.primary.name

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        try:
            vecs = await self.primary.embed(texts)
        except Exception as exc:  # noqa: BLE001 - 向量化失败必须降级，不能影响主链路
            self.degraded = True
            self.last_error = f"{type(exc).__name__}: {exc}"
            return await self.fallback.embed(texts)
        if vecs is None or len(vecs) != len(texts):
            self.degraded = True
            self.last_error = "embedding 返回数量不匹配"
            return await self.fallback.embed(texts)
        return vecs


def cosine_topk(query_vec: np.ndarray, matrix: np.ndarray, top_k: int) -> list[tuple[int, float]]:
    """余弦 TopK。

    矩阵与查询都做过 L2 归一化时，余弦 == 点积，
    直接一次矩阵乘法算出全部相似度（对几百个 chunk 是微秒级），
    比"逐个算余弦"少一整趟 Python 循环。
    """
    if matrix.size == 0 or query_vec.size == 0:
        return []
    q = query_vec.astype(np.float32, copy=False)
    qn = float(np.linalg.norm(q))
    if qn == 0:
        return []
    q = q / qn
    sims = matrix @ q
    order = np.argsort(-sims)[:top_k]
    return [(int(i), float(sims[i])) for i in order]


@dataclass
class VectorHit:
    chunk_id: str
    score: float


class VectorIndex:
    """内存向量索引（蓝图 §3.2：numpy 余弦 + 内存索引，可换 Chroma）。"""

    def __init__(self, embedder: BaseEmbedder | None = None) -> None:
        self.embedder = embedder or HashEmbedder()
        self.chunk_ids: list[str] = []
        self._matrix: np.ndarray | None = None

    @property
    def size(self) -> int:
        return len(self.chunk_ids)

    @property
    def dim(self) -> int:
        return int(self._matrix.shape[1]) if self._matrix is not None else self.embedder.dim

    async def build(self, docs: Sequence[tuple[str, str]]) -> "VectorIndex":
        self.chunk_ids = [cid for cid, _ in docs]
        if not docs:
            self._matrix = np.zeros((0, self.embedder.dim), dtype=np.float32)
            return self
        matrix = await self.embedder.embed([text for _, text in docs])
        self._matrix = np.asarray(matrix, dtype=np.float32)
        return self

    async def search(self, query: str, top_k: int = 5) -> list[VectorHit]:
        if self._matrix is None or self.size == 0:
            return []
        qvec = await self.embedder.embed_query(query)
        pairs = cosine_topk(qvec, self._matrix, top_k)
        return [
            VectorHit(chunk_id=self.chunk_ids[idx], score=score)
            for idx, score in pairs
            if score > 0
        ]

    @staticmethod
    def normalize(matrix: np.ndarray) -> np.ndarray:
        """把矩阵按行 L2 归一化（外部复用/测试用）。"""
        if matrix.size == 0:
            return matrix
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return matrix / norms


class OpenAICompatEmbedder(BaseEmbedder):
    """真实 embedding（OpenAI 兼容 /embeddings 端点）。

    仅在 LLM_PROVIDER=openai_compat 且有 Key 时被构造；构造失败或调用失败
    一律由 FallbackEmbedder 兜到哈希嵌入。
    """

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str = "text-embedding-3-small") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = HASH_DIM  # 真实维度由服务返回后动态更新

    async def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": list(texts)},
            )
            resp.raise_for_status()
            body = resp.json()
        data = sorted(body["data"], key=lambda d: d.get("index", 0))
        matrix = np.asarray([d["embedding"] for d in data], dtype=np.float32)
        self.dim = int(matrix.shape[1]) if matrix.size else self.dim
        return VectorIndex.normalize(matrix)


def min_max_normalize(scores: dict[str, float]) -> dict[str, float]:
    """把一路分数压到 [0,1]，否则 BM25（可能 5~20 分）会完全压死余弦（0~1）。"""
    if not scores:
        return {}
    values = list(scores.values())
    lo, hi = min(values), max(values)
    if math.isclose(hi, lo):
        # 全部同分：统一给 1.0，让权重决策交给另一路召回
        return {k: 1.0 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


__all__ = [
    "BaseEmbedder",
    "HashEmbedder",
    "FallbackEmbedder",
    "OpenAICompatEmbedder",
    "VectorIndex",
    "VectorHit",
    "hash_embed",
    "cosine_topk",
    "min_max_normalize",
    "HASH_DIM",
]
