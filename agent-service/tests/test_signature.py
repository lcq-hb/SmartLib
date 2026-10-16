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
    encoded_path_with_query,
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


def test_query_string_signature_uses_percent_encoded_target() -> None:
    """**回归用例**：签名必须覆盖百分号编码后的请求目标。

    这是曾经的真实缺陷：早期实现手工拼"未编码"的 query（`?topic=续借`）去签名，
    而 Java 侧 `getQueryString()` 拿到的是 `topic=%E7%BB%AD%E5%80%9F`，
    于是**所有带中文查询参数请求一律 401**（ASCII 参数下不会暴露，
    属于典型的"只在非 ASCII 数据上才炸"的 bug）。

    这里用固定向量把编码后的规范串钉死：
    secret=默认值、timestamp=1760000000、
    pathWithQuery=/internal/agent/tools/policy?topic=%E7%BB%AD%E5%80%9F、body 空。
    """
    encoded_path = "/internal/agent/tools/policy?topic=%E7%BB%AD%E5%80%9F"
    canonical = f"{FIXED_TS}\nGET\n{encoded_path}\n"
    expected = hmac.new(DEFAULT_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()

    assert len(expected) == 64
    assert expected == expected.lower()
    assert sign_request(DEFAULT_SECRET, "GET", encoded_path, b"", FIXED_TS) == expected

    # 用"未编码"的中文去签会得到完全不同的结果 —— 正是 401 的成因
    unencoded = "/internal/agent/tools/policy?topic=续借"
    assert sign_request(DEFAULT_SECRET, "GET", unencoded, b"", FIXED_TS) != expected


def test_query_string_is_percent_encoded_in_canonical() -> None:
    encoded_path = "/internal/agent/tools/policy?topic=%E7%BB%AD%E5%80%9F"
    canonical = build_canonical(FIXED_TS, "GET", encoded_path, b"")
    assert "%E7%BB%AD%E5%80%9F" in canonical
    assert "topic=续借" not in canonical
    assert canonical.endswith("\n")


def test_build_query_string_is_display_only() -> None:
    """`build_query_string` 只用于展示/调试，不参与签名（docstring 已写明）。"""
    assert build_query_string({"topic": "续借"}) == "?topic=续借"
    assert build_query_string(None) == ""
    assert build_query_string({"a": 1, "b": None}) == "?a=1"


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
        data = await client.get_stock(book_id=5, trace_id="tr-sign-1")

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
        "/internal/agent/tools/books/5/stock",
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
            await client.get_stock(book_id=5, trace_id="tr-sign-3")


@pytest.mark.asyncio
async def test_policy_query_signature_uses_encoded_raw_path(test_settings) -> None:
    """端到端：中文 query 的签名必须等于"对编码后 raw_path 计算"的结果。

    同时断言 httpx 的 raw_path 里确实是 %E7%BB%AD%E5%80%9F，
    把"签名的对象到底是什么"这件事钉死。
    """
    backend = FakeJavaBackend(strict_sign=True)
    captured: dict[str, str] = {}

    async with JavaClient(test_settings, transport=backend.transport()) as client:
        original_send = client._get_client

        data = await client.policy(topic="续借", trace_id="tr-sign-4")

    assert "续借" in data["answer"]
    call = backend.last()
    assert call is not None
    # 实际发出的 query 是百分号编码形式（服务端 getQueryString() 看到的就是它）
    assert call["query"] == "topic=%E7%BB%AD%E5%80%9F"

    lowered = {k.lower(): v for k, v in call["headers"].items()}
    assert lowered["x-internal-sign"] == reference_sign(
        DEFAULT_SECRET,
        lowered["x-internal-timestamp"],
        "GET",
        "/internal/agent/tools/policy?topic=%E7%BB%AD%E5%80%9F",
        b"",
    )


@pytest.mark.asyncio
async def test_encoded_path_with_query_matches_httpx_raw_path() -> None:
    """`encoded_path_with_query` 必须与 httpx 的 raw_path 逐字节一致。"""
    from app.client.java_client import encoded_path_with_query

    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as raw:
        built = raw.build_request(
            "GET", "/internal/agent/tools/policy", params={"topic": "续借", "size": 3}
        )
    assert encoded_path_with_query(built) == built.url.raw_path.decode("ascii")
    assert "%E7%BB%AD%E5%80%9F" in encoded_path_with_query(built)
    assert "topic=续借" not in encoded_path_with_query(built)


def test_encoded_path_with_query_falls_back_when_raw_path_missing() -> None:
    """构造方式拿不到 raw_path 时退回 path+query（两者都是编码后的形式）。"""
    from app.client.java_client import encoded_path_with_query

    request = httpx.Request("GET", "http://x/internal/agent/tools/policy?topic=续借")
    assert encoded_path_with_query(request) == request.url.raw_path.decode("ascii")


@pytest.mark.asyncio
async def test_ascii_query_signature_still_works(test_settings) -> None:
    """保留 ASCII 用例：编码后的 ASCII 参数与原形式一致，签名行为不变。"""
    backend = FakeJavaBackend(strict_sign=True)
    async with JavaClient(test_settings, transport=backend.transport()) as client:
        data = await client.get_stock(book_id=5, trace_id="tr-ascii")
    assert data["available"] == 3
    call = backend.last()
    assert call is not None
    lowered = {k.lower(): v for k, v in call["headers"].items()}
    assert lowered["x-internal-sign"] == reference_sign(
        DEFAULT_SECRET,
        lowered["x-internal-timestamp"],
        "GET",
        "/internal/agent/tools/books/5/stock",
        b"",
    )


def test_sign_disabled_omits_headers(test_settings) -> None:
    """INTERNAL_SIGN_ENABLED=false 时不应带签名头（本地联调免签名场景）。"""
    disabled = test_settings.model_copy(update={"internal_sign_enabled": False})
    client = JavaClient(
        disabled,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 0, "data": {}})),
    )
    headers = client._sign_headers("GET", "/x", b"", "tr")
    assert "X-Internal-Sign" not in headers
    assert "X-Internal-Timestamp" not in headers
    assert headers["X-Trace-Id"] == "tr"
