"""工具层测试：8 个工具 + 参数强校验 + 越权 + 失败翻译 + 超时/重试/熔断。

关键覆盖点（每一条都对应一个真实的线上风险）：
- 参数校验失败不能变成 500，必须变成"请补充信息"；
- readerId 与会话不一致必须拒绝（越权）；
- 写操作没有 confirm 必须拒绝（二次确认）；
- 后端业务失败（NO_AVAILABLE_COPY）要翻译成中文而不是抛原始 reason；
- 5xx 要重试、连续失败要熔断、熔断期间不再打后端；
- availableCopies=-1 必须表达成"查不到"，不能说成"没有库存"。
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.client.java_client import JavaClient
from app.errors import CircuitOpenError, friendly_reason
from app.tools import registry
from app.tools.base import ToolContext, ToolError, ToolResult
from app.tools.borrow import TOOL as BORROW
from app.tools.get_reader_profile import TOOL as PROFILE
from app.tools.get_stock import TOOL as STOCK
from app.tools.my_loans import TOOL as LOANS
from app.tools.policy_qa import TOOL as POLICY
from app.tools.recommend import TOOL as RECOMMEND
from app.tools.renew import TOOL as RENEW
from app.tools.search_books import TOOL as SEARCH
from tests.conftest import FakeJavaBackend


def ctx_for(settings, backend: FakeJavaBackend, *, reader_id: int | None = 1001) -> ToolContext:
    return ToolContext(
        trace_id="tr-test",
        reader_id=reader_id,
        session_id="s-test",
        java=JavaClient(settings, transport=backend.transport()),
        settings=settings,
    )


# =============================================================================
# 注册表与 schema
# =============================================================================
def test_registry_has_exactly_eight_required_tools() -> None:
    assert set(registry.REQUIRED_TOOLS) == {
        "search_books",
        "get_stock",
        "borrow",
        "renew",
        "my_loans",
        "get_reader_profile",
        "policy_qa",
        "recommend",
    }
    assert registry.missing_required() == []
    assert len(registry.all_tools()) == 8


def test_write_tools_are_flagged() -> None:
    assert BORROW.is_write is True
    assert RENEW.is_write is True
    for tool in (SEARCH, STOCK, LOANS, PROFILE, POLICY, RECOMMEND):
        assert tool.is_write is False


def test_every_tool_exposes_json_schema() -> None:
    for tool in registry.all_tools():
        schema = tool.json_schema()
        assert schema["name"] == tool.name
        assert schema["parameters"]["type"] == "object"
        assert schema["parameters"]["properties"], f"{tool.name} 没有参数 schema"
        spec = tool.llm_spec()
        assert set(spec) == {"name", "description", "parameters"}


def test_tool_names_match_blueprint() -> None:
    """工具名必须与蓝图 §4.3 / §8.2 逐字一致（不一致会让评测与文档对不上）。"""
    names = [t.name for t in registry.all_tools()]
    assert names == list(registry.REQUIRED_TOOLS)


# =============================================================================
# 参数强校验
# =============================================================================
@pytest.mark.parametrize(
    "model,payload",
    [
        (SEARCH.args_model, {"query": ""}),  # 关键词不能空
        (SEARCH.args_model, {"query": "ok", "size": 999}),  # size 上限 50
        (SEARCH.args_model, {"query": "ok", "page": 0}),  # 页码下限 1
        (SEARCH.args_model, {"query": "ok", "sortBy": "hack"}),  # 枚举外排序
        (SEARCH.args_model, {"query": "ok", "unknown": 1}),  # 未知字段（extra=forbid）
        (STOCK.args_model, {"book_id": 0}),  # 主键必须正整数
        (BORROW.args_model, {"readerId": 1, "bookId": 1, "idempotencyKey": "short"}),  # 幂等键太短
        (LOANS.args_model, {}),  # 缺 readerId
        (POLICY.args_model, {"topic": ""}),  # topic 不能空
        (RECOMMEND.args_model, {"limit": 99}),  # limit 上限 10
    ],
)
def test_argument_validation_rejects_bad_input(model, payload) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize(
    "model,payload",
    [
        (SEARCH.args_model, {"query": " 算法导论 "}),
        (SEARCH.args_model, {"query": "x", "size": 50, "page": 100}),
        (STOCK.args_model, {"book_id": 1}),
        (RECOMMEND.args_model, {"limit": 10}),
    ],
)
def test_argument_validation_accepts_boundary_values(model, payload) -> None:
    parsed = model.model_validate(payload)
    assert parsed is not None


def test_search_query_is_stripped() -> None:
    parsed = SEARCH.args_model.model_validate({"query": "  算法导论  "})
    assert parsed.query == "算法导论"


# =============================================================================
# search_books
# =============================================================================
@pytest.mark.asyncio
async def test_search_books_success(test_settings, fake_backend) -> None:
    result = await SEARCH.handle(
        SEARCH.args_model.model_validate({"query": "算法导论"}), ctx_for(test_settings, fake_backend)
    )
    assert result.ok is True
    assert result.data["total"] == 2
    assert result.data["items"][0]["title"] == "算法导论"
    assert result.data["degraded"] is False
    assert "共 2 条结果" in result.summary


@pytest.mark.asyncio
async def test_search_books_degraded_wording(test_settings, fake_backend) -> None:
    """后端降级时必须弱化"结果完整"的表述（degraded + degradeReason）。"""

    original = fake_backend.default_search

    def degraded(query: str):
        payload = original(query)
        payload["degraded"] = True
        payload["degradeReason"] = "es_unavailable_fallback_db"
        return payload

    fake_backend.default_search = degraded  # type: ignore[assignment]
    result = await SEARCH.handle(
        SEARCH.args_model.model_validate({"query": "算法导论"}), ctx_for(test_settings, fake_backend)
    )
    assert result.data["degraded"] is True
    assert result.data["degradeReason"] == "es_unavailable_fallback_db"
    assert "可能不完整" in result.summary
    assert "es_unavailable_fallback_db" in result.summary


@pytest.mark.asyncio
async def test_search_books_empty_result_is_not_an_error(test_settings, fake_backend) -> None:
    fake_backend.default_search = lambda q: {  # type: ignore[assignment]
        "items": [],
        "total": 0,
        "tookMs": 3,
        "engine": "inverted-index",
        "degraded": False,
    }
    result = await SEARCH.handle(
        SEARCH.args_model.model_validate({"query": "不存在的书"}), ctx_for(test_settings, fake_backend)
    )
    assert result.ok is True
    assert result.data["items"] == []
    assert "没有检索到" in result.summary


# =============================================================================
# get_stock（-1 降级语义）
# =============================================================================
@pytest.mark.asyncio
async def test_get_stock_available(test_settings, fake_backend) -> None:
    result = await STOCK.handle(
        STOCK.args_model.model_validate({"book_id": 5}), ctx_for(test_settings, fake_backend)
    )
    assert result.ok is True
    assert result.data["available"] == 3
    assert result.data["indeterminate"] is False
    assert "可借 3 册" in result.summary


@pytest.mark.asyncio
async def test_get_stock_indeterminate_minus_one(test_settings, fake_backend) -> None:
    """availableCopies == -1 是"库存无法确认"，绝不能当成"没有库存"。"""
    fake_backend.stock_available = -1
    result = await STOCK.handle(
        STOCK.args_model.model_validate({"book_id": 5}), ctx_for(test_settings, fake_backend)
    )
    assert result.ok is True
    assert result.data["available"] == -1
    assert result.data["indeterminate"] is True
    assert result.data["degraded"] is True
    assert "无法确认" in result.summary
    assert "没有可借" not in result.summary


@pytest.mark.asyncio
async def test_get_stock_zero_means_can_reserve(test_settings, fake_backend) -> None:
    fake_backend.stock_available = 0
    result = await STOCK.handle(
        STOCK.args_model.model_validate({"book_id": 5}), ctx_for(test_settings, fake_backend)
    )
    assert result.data["available"] == 0
    assert result.data["indeterminate"] is False
    assert "无可借复本" in result.summary


@pytest.mark.asyncio
async def test_get_stock_missing_data_raises_tool_error(test_settings) -> None:
    backend = FakeJavaBackend()
    backend.stock_available = None
    backend.default_search = lambda q: {}  # type: ignore[assignment]

    def empty_stock(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": None, "traceId": "t"})

    ctx = ToolContext(
        trace_id="t",
        reader_id=1001,
        java=JavaClient(test_settings, transport=httpx.MockTransport(empty_stock)),
        settings=test_settings,
    )
    with pytest.raises(ToolError) as exc:
        await STOCK.handle(STOCK.args_model.model_validate({"book_id": 999}), ctx)
    assert exc.value.code == "BOOK_NOT_FOUND"
    assert "馆藏信息" in exc.value.user_message


# =============================================================================
# borrow / renew（写操作）
# =============================================================================
@pytest.mark.asyncio
async def test_borrow_requires_confirmation(test_settings, fake_backend) -> None:
    with pytest.raises(ToolError) as exc:
        await BORROW.handle(
            BORROW.args_model.model_validate(
                {"readerId": 1001, "bookId": 5, "idempotencyKey": "k" * 10, "confirm": False}
            ),
            ctx_for(test_settings, fake_backend),
        )
    assert exc.value.code == "CONFIRMATION_REQUIRED"
    assert "确认" in exc.value.user_message
    # 关键：没有确认时**一个后端请求都不该发出**
    assert fake_backend.paths() == []


@pytest.mark.asyncio
async def test_borrow_rejects_other_reader(test_settings, fake_backend) -> None:
    """越权借书：即使护栏被绕过，工具自己也必须拒绝。"""
    with pytest.raises(ToolError) as exc:
        await BORROW.handle(
            BORROW.args_model.model_validate(
                {"readerId": 2002, "bookId": 5, "idempotencyKey": "k" * 10, "confirm": True}
            ),
            ctx_for(test_settings, fake_backend, reader_id=1001),
        )
    assert exc.value.code == "FORBIDDEN_OPERATION"
    assert fake_backend.paths() == []


@pytest.mark.asyncio
async def test_borrow_without_reader_identity_asks_for_it(test_settings, fake_backend) -> None:
    with pytest.raises(ToolError) as exc:
        await BORROW.handle(
            BORROW.args_model.model_validate(
                {"readerId": 1001, "bookId": 5, "idempotencyKey": "k" * 10, "confirm": True}
            ),
            ctx_for(test_settings, fake_backend, reader_id=None),
        )
    assert exc.value.code == "READER_REQUIRED"
    assert "读者编号" in exc.value.user_message


@pytest.mark.asyncio
async def test_borrow_success_passes_reader_and_idempotency_key(test_settings, fake_backend) -> None:
    result = await BORROW.handle(
        BORROW.args_model.model_validate(
            {"readerId": 1001, "bookId": 5, "idempotencyKey": "idem-0001", "confirm": True}
        ),
        ctx_for(test_settings, fake_backend),
    )
    assert result.ok is True
    assert result.data["borrowId"] == 9001
    assert result.data["dueDate"] == "2026-04-01"

    sent = fake_backend.last()
    assert sent is not None
    body = sent["body"].decode("utf-8")
    assert '"readerId":1001' in body
    assert '"idempotencyKey":"idem-0001"' in body


@pytest.mark.asyncio
async def test_borrow_translates_backend_reason_to_chinese(test_settings, fake_backend) -> None:
    fake_backend.borrow_ok = False
    fake_backend.borrow_reason = "NO_AVAILABLE_COPY"
    with pytest.raises(ToolError) as exc:
        await BORROW.handle(
            BORROW.args_model.model_validate(
                {"readerId": 1001, "bookId": 5, "idempotencyKey": "k" * 10, "confirm": True}
            ),
            ctx_for(test_settings, fake_backend),
        )
    assert exc.value.code == "NO_AVAILABLE_COPY"
    assert "预约" in exc.value.user_message  # 话术里给出了替代做法
    assert "NO_AVAILABLE_COPY" not in exc.value.user_message  # 不暴露内部码


@pytest.mark.asyncio
async def test_borrow_timeout_says_result_unknown(test_settings) -> None:
    """超时 ≠ 失败：必须告诉读者"不确定是否成功，请自行核对"。"""

    def timeout_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    ctx = ToolContext(
        trace_id="t",
        reader_id=1001,
        java=JavaClient(test_settings, transport=httpx.MockTransport(timeout_transport)),
        settings=test_settings,
    )
    with pytest.raises(ToolError) as exc:
        await BORROW.handle(
            BORROW.args_model.model_validate(
                {"readerId": 1001, "bookId": 5, "idempotencyKey": "k" * 10, "confirm": True}
            ),
            ctx,
        )
    assert exc.value.code == "BORROW_RESULT_UNKNOWN"
    assert "无法确认" in exc.value.user_message
    assert "核对" in exc.value.user_message


@pytest.mark.asyncio
async def test_renew_requires_borrow_id_not_book_id(test_settings, fake_backend) -> None:
    """renew 的入参是 borrowId；传 0 或缺失必须被 schema 拒绝。"""
    with pytest.raises(ValidationError):
        RENEW.args_model.model_validate({"readerId": 1001, "borrowId": 0, "idempotencyKey": "k" * 10})
    with pytest.raises(ValidationError):
        RENEW.args_model.model_validate({"readerId": 1001, "idempotencyKey": "k" * 10})


@pytest.mark.asyncio
async def test_renew_success(test_settings, fake_backend) -> None:
    result = await RENEW.handle(
        RENEW.args_model.model_validate(
            {"readerId": 1001, "borrowId": 5001, "idempotencyKey": "idem-r-1", "confirm": True}
        ),
        ctx_for(test_settings, fake_backend),
    )
    assert result.ok is True
    assert result.data["newDueDate"] == "2026-05-01"
    assert "2026-05-01" in result.summary


@pytest.mark.asyncio
async def test_renew_rejected_by_backend_reason(test_settings, fake_backend) -> None:
    fake_backend.renew_ok = False
    fake_backend.renew_reason = "RENEW_NOT_ALLOWED_RESERVED"
    with pytest.raises(ToolError) as exc:
        await RENEW.handle(
            RENEW.args_model.model_validate(
                {"readerId": 1001, "borrowId": 5001, "idempotencyKey": "k" * 11, "confirm": True}
            ),
            ctx_for(test_settings, fake_backend),
        )
    assert exc.value.code == "RENEW_NOT_ALLOWED_RESERVED"
    assert "预约" in exc.value.user_message


# =============================================================================
# my_loans / get_reader_profile
# =============================================================================
@pytest.mark.asyncio
async def test_my_loans_normalizes_and_filters(test_settings, fake_backend) -> None:
    result = await LOANS.handle(
        LOANS.args_model.model_validate({"readerId": 1001, "only_overdue": False}),
        ctx_for(test_settings, fake_backend),
    )
    assert result.data["count"] == 2
    assert result.data["overdueCount"] == 1
    assert result.data["items"][0]["borrowId"] == 5001
    assert result.data["items"][1]["overdueDays"] == 12

    overdue_only = await LOANS.handle(
        LOANS.args_model.model_validate({"readerId": 1001, "only_overdue": True}),
        ctx_for(test_settings, fake_backend),
    )
    assert overdue_only.data["count"] == 1
    assert overdue_only.data["items"][0]["title"] == "计算机网络"


@pytest.mark.asyncio
async def test_my_loans_rejects_other_reader(test_settings, fake_backend) -> None:
    with pytest.raises(ToolError) as exc:
        await LOANS.handle(
            LOANS.args_model.model_validate({"readerId": 2002}),
            ctx_for(test_settings, fake_backend, reader_id=1001),
        )
    assert exc.value.code == "FORBIDDEN_OPERATION"
    assert "本人" in exc.value.user_message


@pytest.mark.asyncio
async def test_reader_profile_adds_quota_from_blueprint_rules(test_settings, fake_backend) -> None:
    """额度必须按蓝图 §2.1 的规则表补齐（本科生 10 册/30 天/2 次）。"""
    result = await PROFILE.handle(
        PROFILE.args_model.model_validate({"readerId": 1001}), ctx_for(test_settings, fake_backend)
    )
    assert result.data["readerType"] == "UNDERGRAD"
    assert result.data["readerTypeLabel"] == "本科生"
    assert result.data["quota"] == {"maxBooks": 10, "loanDays": 30, "maxRenew": 2}
    assert result.data["canBorrow"] is True
    assert "还可借 8 册" in result.summary


@pytest.mark.parametrize(
    "reader_type,expected",
    [
        ("UNDERGRAD", (10, 30, 2)),
        ("POSTGRAD", (15, 60, 3)),
        ("TEACHER", (20, 90, 3)),
    ],
)
@pytest.mark.asyncio
async def test_reader_type_quota_matrix(test_settings, fake_backend, reader_type, expected) -> None:
    fake_backend.profile = {
        "readerType": reader_type,
        "creditScore": 100,
        "preferredCategories": [],
        "borrowedCount": 0,
    }
    result = await PROFILE.handle(
        PROFILE.args_model.model_validate({"readerId": 1001}), ctx_for(test_settings, fake_backend)
    )
    quota = result.data["quota"]
    assert (quota["maxBooks"], quota["loanDays"], quota["maxRenew"]) == expected


@pytest.mark.asyncio
async def test_reader_profile_flags_low_credit(test_settings, fake_backend) -> None:
    fake_backend.profile = {
        "readerType": "UNDERGRAD",
        "creditScore": 55,
        "preferredCategories": [],
        "borrowedCount": 3,
    }
    result = await PROFILE.handle(
        PROFILE.args_model.model_validate({"readerId": 1001}), ctx_for(test_settings, fake_backend)
    )
    assert result.data["canBorrow"] is False
    assert "暂不可借" in result.summary


# =============================================================================
# policy_qa（三级降级）
# =============================================================================
@pytest.mark.asyncio
async def test_policy_qa_prefers_rule_engine(test_settings, fake_backend) -> None:
    """规则库可用时必须以它为准：原样采用后端答案、给出后端出处，且只打一次后端。"""
    result = await POLICY.handle(
        POLICY.args_model.model_validate({"topic": "本科生能借几本"}),
        ctx_for(test_settings, fake_backend),
    )
    assert result.ok is True
    assert result.data["retrieval"] == "rule_engine"
    assert result.data["answer"].startswith("（规则库直答）")
    assert result.data["source"] == "rule_config:borrow.limit.UNDERGRAD"
    assert fake_backend.paths() == ["/internal/agent/tools/policy"]


@pytest.mark.asyncio
async def test_policy_qa_falls_back_to_local_rag_when_backend_down(test_settings) -> None:
    """规则库不可用时必须能回答（这是"零依赖可跑"的关键路径）。"""

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": 503, "msg": "DOWN"})

    ctx = ToolContext(
        trace_id="t",
        reader_id=None,
        java=JavaClient(test_settings, transport=httpx.MockTransport(failing)),
        settings=test_settings,
    )
    result = await POLICY.handle(
        POLICY.args_model.model_validate({"topic": "逾期罚金 0.2 元 怎么算"}), ctx
    )
    assert result.ok is True
    assert result.data["retrieval"] == "hybrid_bm25_vector"
    assert result.data["degraded"] is True
    assert result.data["chunks"], "本地 RAG 必须召回知识库片段"
    assert "罚金" in result.data["answer"] or "0.2" in result.data["answer"]


@pytest.mark.asyncio
async def test_policy_qa_raises_when_nothing_matches(test_settings) -> None:
    """规则库挂了 + 知识库确实没这条：必须明确说"没查到"并给出替代做法。

    这条同时守住了 RAG 的**可信度下限**：不能因为 BM25 能返回点什么，
    就把"图书馆是否提供二十四小时天文台预约"答成"研讨间可以预约"。
    """

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": 503, "msg": "DOWN"})

    ctx = ToolContext(
        trace_id="t",
        reader_id=None,
        java=JavaClient(test_settings, transport=httpx.MockTransport(failing)),
        settings=test_settings,
    )
    with pytest.raises(ToolError) as exc:
        await POLICY.handle(
            POLICY.args_model.model_validate({"topic": "图书馆是否提供二十四小时天文台预约"}), ctx
        )
    assert exc.value.code == "POLICY_NOT_FOUND"
    assert "服务台" in exc.value.user_message


# =============================================================================
# recommend（三级策略）
# =============================================================================
@pytest.mark.asyncio
async def test_recommend_by_seed_book(test_settings, fake_backend) -> None:
    result = await RECOMMEND.handle(
        RECOMMEND.args_model.model_validate({"book_id": 5, "limit": 2}),
        ctx_for(test_settings, fake_backend),
    )
    assert result.data["strategy"] == "similar_books"
    assert len(result.data["items"]) == 2
    assert all(item["reason"] for item in result.data["items"])


@pytest.mark.asyncio
async def test_recommend_falls_back_to_hot(test_settings, fake_backend) -> None:
    """相似书接口挂了也要能给推荐（走热门榜兜底），并标记 degraded。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/similar" in request.url.path:
            return httpx.Response(500, json={"code": 500, "msg": "BOOM"})
        return fake_backend.handle(request)

    ctx = ToolContext(
        trace_id="t",
        reader_id=1001,
        java=JavaClient(test_settings, transport=httpx.MockTransport(handler)),
        settings=test_settings,
    )
    result = await RECOMMEND.handle(
        RECOMMEND.args_model.model_validate({"book_id": 5, "limit": 2}), ctx
    )
    assert result.data["strategy"] == "hot_fallback"
    assert result.data["degraded"] is True


@pytest.mark.asyncio
async def test_recommend_raises_when_all_paths_fail(test_settings) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"code": 503, "msg": "DOWN"})

    ctx = ToolContext(
        trace_id="t",
        reader_id=1001,
        java=JavaClient(test_settings, transport=httpx.MockTransport(failing)),
        settings=test_settings,
    )
    with pytest.raises(ToolError) as exc:
        await RECOMMEND.handle(RECOMMEND.args_model.model_validate({"limit": 2}), ctx)
    assert exc.value.code == "RECOMMEND_UNAVAILABLE"


# =============================================================================
# 失败翻译表
# =============================================================================
@pytest.mark.parametrize(
    "reason,expected_fragment",
    [
        ("CREDIT_TOO_LOW", "信用分"),
        ("HAS_OVERDUE", "逾期"),
        ("BORROW_LIMIT_EXCEEDED", "可借册数"),
        ("DUPLICATE_BORROW", "已经有一册"),
        ("FINE_UNPAID", "罚金"),
        ("RENEW_LIMIT_EXCEEDED", "续借次数"),
        ("RENEW_NOT_ALLOWED_OVERDUE", "逾期"),
        ("NO_AVAILABLE_COPY", "预约"),
    ],
)
def test_friendly_reason_covers_business_codes(reason: str, expected_fragment: str) -> None:
    message = friendly_reason(reason)
    assert expected_fragment in message
    assert reason not in message


def test_friendly_reason_handles_unknown_code() -> None:
    assert friendly_reason("SOME_NEW_CODE") == "操作没有成功，请稍后再试。"
    assert friendly_reason(None).startswith("操作没有成功")


# =============================================================================
# 重试与熔断
# =============================================================================
@pytest.mark.asyncio
async def test_get_request_retries_on_5xx(test_settings, fake_backend) -> None:
    """GET 是幂等安全的：5xx 必须重试（默认 2 次，共 3 次尝试）。"""
    fake_backend.fail_times = 2
    async with JavaClient(test_settings, transport=fake_backend.transport()) as client:
        data = await client.get_stock(book_id=5, trace_id="t")
    assert data["available"] == 3
    assert len(fake_backend.calls) == 3


@pytest.mark.asyncio
async def test_post_without_idempotency_check_still_retries_when_marked_idempotent(
    test_settings, fake_backend
) -> None:
    """带幂等键的写操作允许重试（后端靠 idempotent_record 去重）。"""
    fake_backend.fail_times = 1
    async with JavaClient(test_settings, transport=fake_backend.transport()) as client:
        result = await client.borrow(
            reader_id=1001, book_id=5, idempotency_key="idem-x", trace_id="t"
        )
    assert result["ok"] is True
    assert len(fake_backend.calls) == 2


@pytest.mark.asyncio
async def test_circuit_opens_after_consecutive_failures(test_settings, fake_backend) -> None:
    fake_backend.always_fail = True
    client = JavaClient(test_settings, transport=fake_backend.transport())
    with pytest.raises(Exception):
        await client.get_stock(book_id=5, trace_id="t")
    # 熔断阈值默认 5，但每次调用内部还会重试 3 次；用直接调 request 的方式精确计数
    client.breaker.reset()
    fake_backend.clear()
    for _ in range(test_settings.java_circuit_fail_threshold):
        await client.breaker.record_failure()
    assert client.breaker.state == "OPEN"
    calls_before = len(fake_backend.calls)
    with pytest.raises(CircuitOpenError):
        await client.get_stock(book_id=5, trace_id="t")
    # 熔断打开后**不再打后端**（快速失败，保护上游）
    assert len(fake_backend.calls) == calls_before
    await client.aclose()


@pytest.mark.asyncio
async def test_circuit_half_open_then_closed(test_settings, fake_backend) -> None:
    """三态迁移：OPEN →（冷却结束）HALF_OPEN → 探测成功 → CLOSED。"""
    client = JavaClient(test_settings, transport=fake_backend.transport())
    for _ in range(test_settings.java_circuit_fail_threshold):
        await client.breaker.record_failure()
    assert client.breaker.state == "OPEN"

    # 把冷却时间拨到过去，模拟"等够了"
    client.breaker.opened_at = 0.0
    assert await client.breaker.allow() is True
    assert client.breaker.state == "HALF_OPEN"

    await client.breaker.record_success()
    assert client.breaker.state == "CLOSED"
    assert client.breaker.consecutive_failures == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_half_open_probes_are_capped(test_settings) -> None:
    backend = FakeJavaBackend()
    client = JavaClient(test_settings, transport=backend.transport())
    for _ in range(test_settings.java_circuit_fail_threshold):
        await client.breaker.record_failure()
    client.breaker.opened_at = 0.0
    allowed = 0
    for _ in range(test_settings.java_circuit_half_open_probes + 3):
        if await client.breaker.allow():
            allowed += 1
    assert allowed == test_settings.java_circuit_half_open_probes
    await client.aclose()


@pytest.mark.asyncio
async def test_4xx_does_not_retry(test_settings, fake_backend) -> None:
    """4xx 是契约/权限问题，重试无意义，必须只打一次。"""
    from app.errors import UpstreamError

    fake_backend.status_code_times = {400: 5}
    async with JavaClient(test_settings, transport=fake_backend.transport()) as client:
        with pytest.raises(UpstreamError):
            await client.get_stock(book_id=5, trace_id="t")
    assert len(fake_backend.calls) == 1


@pytest.mark.asyncio
async def test_trace_id_is_propagated_to_backend(test_settings, fake_backend) -> None:
    async with JavaClient(test_settings, transport=fake_backend.transport()) as client:
        await client.my_loans(reader_id=1001, trace_id="tr-propagation-check")
    sent = fake_backend.last()
    assert sent is not None
    assert sent["headers"].get("x-trace-id") == "tr-propagation-check"


# =============================================================================
# ToolResult 形状
# =============================================================================
def test_tool_result_observation_shape() -> None:
    ok = ToolResult(ok=True, tool="get_stock", data={"available": 1}, summary="有 1 册")
    assert ok.to_observation() == {
        "tool": "get_stock",
        "ok": True,
        "data": {"available": 1},
        "summary": "有 1 册",
    }
    bad = ToolResult(ok=False, tool="borrow", error_code="X", error_message="不行")
    payload = bad.to_observation()
    assert payload["ok"] is False
    assert payload["error_code"] == "X"
    assert "data" not in payload
