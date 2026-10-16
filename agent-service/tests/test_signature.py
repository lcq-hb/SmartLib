"""签名协议测试：与后端 InternalSignFilter 的固定向量对齐。

这是整个联调里最容易出错、也最难排查的一环（签名不对只会得到一个 401），
所以这里不测"能不能跑"，而是测**逐字一致的规范串**：
- 独立实现一遍后端规格（tests/conftest.py 的 reference_sign），断言两边输出相同；
- 固定向量：secret 用默认值、timestamp=1760000000、POST、/internal/agent/tools/borrow、
  body={"readerId":1,"bookId":1}，断言 64 位小写 hex；
- 覆盖 4 个易错点：GET 无 body 仍保留末尾换行、query 不做 URL 编码、METHOD 大写、LF 而非 CRLF。
"""

from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest

from app.client.java_client import (
    JavaClient,
    build_canonical,
    build_query_string,
    build_sign_headers,
    compute_sign,
    sign_request,
)
from tests.conftest import DEFAULT_SECRET, FakeJavaBackend, reference_sign

FIXED_TS = "1760000000"
BORROW_PATH = "/internal/agent/tools/borrow"
BORROW_BODY = b'{"readerId":1,"bookId":1}'


# =============================================================================
# 固定向量
# =============================================================================
def test_sign_fixed_vector_is_64_lowercase_hex() -> None:
    sign = sign_request(DEFAULT_SECRET, "POST", BORROW_PATH, BORROW_BODY, FIXED_TS)
    assert len(sign) == 64
    assert sign == sign.lower()
    assert all(ch in "0123456789abcdef" for ch in sign)


def test_sign_matches_backend_reference_implementation() -> None:
    """与参考实现逐字一致（参考实现照后端规格写，不引用业务代码）。"""
    mine = sign_request(DEFAULT_SECRET, "POST", BORROW_PATH, BORROW_BODY, FIXED_TS)
    theirs = reference_sign(DEFAULT_SECRET, FIXED_TS, "POST", BORROW_PATH, BORROW_BODY)
    assert mine == theirs


def test_canonical_string_shape() -> None:
    """规范串 = timestamp \\n METHOD \\n pathWithQuery \\n bodyUtf8，且以 \\n 结尾。"""
    canonical = build_canonical(FIXED_TS, "post", BORROW_PATH, BORROW_BODY)
    assert canonical == f"{FIXED_TS}\nPOST\n{BORROW_PATH}\n" + BORROW_BODY.decode("utf-8")
    assert canonical.count("\n") == 3
    assert canonical.endswith(BORROW_BODY.decode("utf-8"))
    assert "\r" not in canonical


def test_get_without_body_keeps_trailing_newline() -> None:
    """GET 无 body：bodyUtf8 为空串，但末尾换行必须保留（4 段 3 换行）。"""
    canonical = build_canonical(FIXED_TS, "GET", "/internal/agent/tools/policy", b"")
    assert canonical == f"{FIXED_TS}\nGET\n/internal/agent/tools/policy\n"
    assert canonical.endswith("\n")
    assert canonical.count("\n") == 3


def test_query_string_is_not_url_encoded_for_signature() -> None:
    """签名串里的 topic 必须是原始中文；实际请求 URL 才编码。"""
    qs = build_query_string({"topic": "续借"})
    assert qs == "?topic=续借"
    assert "%" not in qs

    path_with_query = "/internal/agent/tools/policy" + qs
    canonical = build_canonical(FIXED_TS, "GET", path_with_query, b"")
    assert "topic=续借" in canonical
    # 若错误地做了 URL 编码，签名必然与后端不一致
    assert "topic=%E7%BB" not in canonical


def test_sign_uses_hmac_sha256_lowercase_hex() -> None:
    """手工复算一遍，确保没有多加盐、没有用 base64。"""
    canonical = f"{FIXED_TS}\nPOST\n{BORROW_PATH}\n{BORROW_BODY.decode()}"
    expected = hmac.new(
        DEFAULT_SECRET.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    assert compute_sign(DEFAULT_SECRET, canonical) == expected
    assert sign_request(DEFAULT_SECRET, "POST", BORROW_PATH, BORROW_BODY, FIXED_TS) == expected


def test_build_sign_headers_contains_both_headers() -> None:
    headers = build_sign_headers(DEFAULT_SECRET, "POST", BORROW_PATH, BORROW_BODY, timestamp=FIXED_TS)
    assert headers["X-Internal-Timestamp"] == FIXED_TS
    assert len(headers["X-Internal-Sign"]) == 64


def test_sign_is_sensitive_to_every_component() -> None:
    """任何一段变了签名都必须变——这是"签名真的覆盖了全部要素"的证明。"""
    base = sign_request(DEFAULT_SECRET, "POST", BORROW_PATH, BORROW_BODY, FIXED_TS)
    assert sign_request("other-secret", "POST", BORROW_PATH, BORROW_BODY, FIXED_TS) != base
    assert sign_request(DEFAULT_SECRET, "GET", BORROW_PATH, BORROW_BODY, FIXED_TS) != base
    assert sign_request(DEFAULT_SECRET, "POST", BORROW_PATH + "x", BORROW_BODY, FIXED_TS) != base
    assert sign_request(DEFAULT_SECRET, "POST", BORROW_PATH, b"{}", FIXED_TS) != base
    assert sign_request(DEFAULT_SECRET, "POST", BORROW_PATH, BORROW_BODY, "1760000001") != base


# =============================================================================
# 端到端：请求真的带上正确的头，且假后端校验通过
# =============================================================================
@pytest.mark.asyncio
async def test_client_sends_valid_signature_headers(test_settings) -> None:
    backend = FakeJavaBackend(strict_sign=True)
    async with JavaClient(test_settings, transport=backend.transport()) as client:
        data = await client.get_stock(book_id=101, trace_id="tr-sign-1")

    assert data["available"] == 3
    call = backend.last()
    assert call is not None
    headers = call["headers"]
    # httpx 的 header 名大小写不敏感，统一按小写取
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered["x-trace-id"] == "tr-sign-1"
    assert lowered["x-internal-sign"] == reference_sign(
        DEFAULT_SECRET,
        lowered["x-internal-timestamp"],
        "GET",
        "/internal/agent/tools/books/101/stock",
        b"",
    )


@pytest.mark.asyncio
async def test_client_post_body_signature_matches_sent_bytes(test_settings) -> None:
    backend = FakeJavaBackend(strict_sign=True)
    async with JavaClient(test_settings, transport=backend.transport()) as client:
        await client.borrow(
            reader_id=1, book_id=1, idempotency_key="idem-key-0001", trace_id="tr-sign-2"
        )

    call = backend.last()
    assert call is not None
    lowered = {k.lower(): v for k, v in call["headers"].items()}
    # 签名必须覆盖"真正发出去的字节"，而不是重新序列化一遍的结果
    assert lowered["x-internal-sign"] == reference_sign(
        DEFAULT_SECRET,
        lowered["x-internal-timestamp"],
        "POST",
        "/internal/agent/tools/borrow",
        call["body"],
    )


@pytest.mark.asyncio
async def test_wrong_secret_is_rejected_by_backend(test_settings) -> None:
    """反证：换掉 secret 后假后端返回 401 → 客户端抛错（说明校验真的在起作用）。"""
    from app.errors import UpstreamError

    backend = FakeJavaBackend(secret="another-secret")
    async with JavaClient(test_settings, transport=backend.transport()) as client:
        with pytest.raises(UpstreamError):
            await client.get_stock(book_id=101, trace_id="tr-sign-3")


@pytest.mark.asyncio
async def test_policy_query_signature_includes_raw_chinese(test_settings) -> None:
    backend = FakeJavaBackend(strict_sign=True)
    async with JavaClient(test_settings, transport=backend.transport()) as client:
        data = await client.policy(topic="续借", trace_id="tr-sign-4")

    assert "续借" in data["answer"]
    call = backend.last()
    assert call is not None
    # 实际发出的 URL query 是编码后的，但签名用的是未编码形式
    assert "topic=%E7%BB%AD%E5%80%9F" in call["query"]
    lowered = {k.lower(): v for k, v in call["headers"].items()}
    assert lowered["x-internal-sign"] == reference_sign(
        DEFAULT_SECRET,
        lowered["x-internal-timestamp"],
        "GET",
        "/internal/agent/tools/policy?topic=续借",
        b"",
    )


def test_sign_disabled_omits_headers(test_settings) -> None:
    """INTERNAL_SIGN_ENABLED=false 时不应带签名头（本地联调免签名场景）。"""
    disabled = test_settings.model_copy(update={"internal_sign_enabled": False})
    client = JavaClient(disabled, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 0, "data": {}})))
    headers = client._headers("tr", b"", "GET", "/x")
    assert "X-Internal-Sign" not in headers
    assert "X-Internal-Timestamp" not in headers
    assert headers["X-Trace-Id"] == "tr"
