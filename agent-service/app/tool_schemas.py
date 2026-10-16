"""所有工具的 Pydantic 参数模型集中定义。

为什么集中而不是散在每个工具文件里：
- 蓝图 §8.2 的 8 条内部 API 入参是一张表，集中定义能一眼核对"代码与契约是否逐字一致"；
- 工具 schema 要同时给 /tools 接口、guardrail 参数校验、LLM function calling 使用，
  放一处避免三份定义漂移。

校验原则（强校验，宁可提前拦下也不要打到后端）：
- 读者/书籍 ID 必须是正整数（后端主键是 bigint 自增）；
- 分页 size 上限 50，防止一次把全馆书目拉回来打爆后端；
- 页码下限 1，上限 100（深分页在自研倒排索引上是反模式，蓝图 §6.2 的 TopK 设计）；
- 书名关键词非空且去首尾空格；
- 幂等键长度限制，避免超长 key 撑爆 idempotent_record.idem_key 索引。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    """所有工具参数的基类：禁止未声明字段，避免 LLM 幻觉出多余参数被静默忽略。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchBooksArgs(_StrictModel):
    """search_books：POST /internal/agent/tools/search-books"""

    query: str = Field(..., min_length=1, max_length=80, description="检索关键词，如书名/作者/ISBN")
    category: str | None = Field(None, max_length=40, description="分类名或分类编码，可选")
    page: int = Field(1, ge=1, le=100, description="页码，从 1 开始")
    size: int = Field(10, ge=1, le=50, description="每页条数，最大 50")
    sortBy: Literal["relevance", "hot", "newest"] = Field(
        "relevance", description="排序：相关度/热度/最新"
    )

    @field_validator("query")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("检索关键词不能为空")
        return v.strip()


class GetStockArgs(_StrictModel):
    """get_stock：GET /internal/agent/tools/books/{id}/stock"""

    book_id: int = Field(..., ge=1, description="书目 ID")


class BorrowArgs(_StrictModel):
    """borrow：POST /internal/agent/tools/borrow（写操作，必须二次确认 + 幂等键）"""

    readerId: int = Field(..., ge=1, description="读者 ID，必须与当前会话读者一致")
    bookId: int = Field(..., ge=1, description="书目 ID")
    idempotencyKey: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="幂等键，同一次借阅重试必须复用同一个值",
    )
    confirm: bool = Field(
        False,
        description="二次确认标记：用户明确说“确认”后才为 true，护栏据此放行写操作",
    )


class RenewArgs(_StrictModel):
    """renew：POST /internal/agent/tools/renew（写操作）"""

    readerId: int = Field(..., ge=1, description="读者 ID，必须与当前会话读者一致")
    borrowId: int = Field(..., ge=1, description="借阅记录 ID（不是书目 ID）")
    idempotencyKey: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="幂等键，同一次续借重试必须复用同一个值",
    )
    confirm: bool = Field(False, description="二次确认标记")


class MyLoansArgs(_StrictModel):
    """my_loans：GET /internal/agent/tools/readers/{id}/loans"""

    readerId: int = Field(..., ge=1, description="读者 ID，必须与当前会话读者一致")
    only_overdue: bool = Field(False, description="只看已逾期的记录")


class GetReaderProfileArgs(_StrictModel):
    """get_reader_profile：GET /internal/agent/tools/readers/{id}/profile"""

    readerId: int = Field(..., ge=1, description="读者 ID，必须与当前会话读者一致")


class PolicyQaArgs(_StrictModel):
    """policy_qa：GET /internal/agent/tools/policy?topic=..."""

    topic: str = Field(..., min_length=1, max_length=120, description="政策问题或主题关键词")
    top_k: int = Field(3, ge=1, le=8, description="召回条数")


class RecommendArgs(_StrictModel):
    """recommend：基于 /api/books/{id}/similar 与 /api/books/hot 做推荐。

    蓝图 §8.2 未单列 recommend 的内部 API，因此这里复用后端的公开相似书/热门榜接口，
    并在 java_client 中走 /internal/agent/tools/books/{id}/similar 与 .../hot 两条内部路径。
    """

    book_id: int | None = Field(None, ge=1, description="以这本书为种子做同类推荐，可选")
    category: str | None = Field(None, max_length=40, description="按分类推荐，可选")
    limit: int = Field(3, ge=1, le=10, description="推荐数量")
