"""调用 Java 后端内部工具接口的统一客户端。

职责（蓝图 §6.4 工程化护栏）：
1. **签名**：每个请求带 `X-Trace-Id`、`X-Internal-Timestamp`（秒级 Unix 时间戳）
   与 `X-Internal-Sign`。规范串与后端 `InternalSignFilter` **逐字对齐**：
       canonical = f"{timestamp}\\n{METHOD}\\n{pathWithQuery}\\n{bodyUtf8}"
       X-Internal-Sign = hex_lowercase(HMAC_SHA256(secret, canonical))
   四个易错点：timestamp 必须与请求头同字符串；METHOD 大写；
   **pathWithQuery 是服务端能看到的、百分号编码后的请求目标**
   （`getRequestURI() + "?" + getQueryString()`，如 `topic=%E7%BB%AD%E5%80%9F`）；
   无 body 时 bodyUtf8 为空串但仍保留末尾换行。
   实现上是"先 build_request → 读 raw_path 签名 → 再 send"，
   保证"签的就是发的"；手工拼编码规则一定会与 httpx 产生偏差。
   后端容忍 ±300 秒时间偏差（application.yml 的 timestamp-tolerance-seconds），
   超出即视为重放。
2. **超时**：默认 3s（蓝图 §6.4）。超时预算覆盖"全部重试"，而不是每次重试各 3s，
   否则一次用户请求最坏会挂 9s，首 Token 延迟 SLO（<1.5s）直接崩。
3. **重试**：默认 2 次，只重试**幂等安全**的请求（GET 与带 idempotencyKey 的 POST）。
   写操作无幂等键时绝不自动重试——这是业务正确性问题，不是性能问题。
4. **熔断**：三态 CLOSED/OPEN/HALF_OPEN，与后端 §6.3 A5 的语义对齐。
5. **trace 透传**：trace_id 由 Agent 服务生成（或沿用上游传入），一路带到 Java，
   保证一次对话能在两侧日志里串起来（蓝图 §11 trace_id 定义）。
6. **错误翻译**：HTTP/业务错误统一转成 errors.py 里的中文友好话术。

可测试性：通过 `transport` 参数注入 httpx.MockTransport，
所以 pytest 完全不需要启动真实 Java 后端。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import time
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from app.config import Settings, get_settings
from app.errors import (
    CircuitOpenError,
    SmartLibError,
    UpstreamError,
    UpstreamTimeoutError,
    friendly_reason,
)


class CircuitBreaker:
    """自研熔断器：CLOSED → OPEN → HALF_OPEN 三态。

    为什么不用 pybreaker 之类：状态机只有 30 行，多一个依赖不值得；
    而且我们要把"当前状态"直接暴露到 /admin/metrics 里做演示。
    """

    def __init__(
        self,
        *,
        fail_threshold: int = 5,
        open_seconds: float = 10.0,
        half_open_probes: int = 3,
    ) -> None:
        self.fail_threshold = fail_threshold
        self.open_seconds = open_seconds
        self.half_open_probes = half_open_probes
        self.state: str = "CLOSED"
        self.consecutive_failures = 0
        self.opened_at = 0.0
        self.half_open_inflight = 0
        self.total_calls = 0
        self.total_failures = 0
        self.short_circuited = 0
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "short_circuited": self.short_circuited,
            "opened_at": self.opened_at or None,
        }

    async def allow(self) -> bool:
        """是否放行本次调用。"""
        async with self._lock:
            now = time.time()
            if self.state == "OPEN":
                if now - self.opened_at >= self.open_seconds:
                    # 冷却结束，进入半开：放少量探针出去试水
                    self.state = "HALF_OPEN"
                    self.half_open_inflight = 0
                else:
                    self.short_circuited += 1
                    return False
            if self.state == "HALF_OPEN":
                if self.half_open_inflight >= self.half_open_probes:
                    # 探针已满，其余请求快速失败，避免半开状态被流量打穿
                    self.short_circuited += 1
                    return False
                self.half_open_inflight += 1
            return True

    async def record_success(self) -> None:
        async with self._lock:
            self.total_calls += 1
            self.consecutive_failures = 0
            if self.state == "HALF_OPEN":
                self.state = "CLOSED"
                self.half_open_inflight = 0

    async def record_failure(self) -> None:
        async with self._lock:
            self.total_calls += 1
            self.total_failures += 1
            self.consecutive_failures += 1
            if self.state == "HALF_OPEN":
                # 半开探针失败 → 立刻回到 OPEN 重新冷却（标准行为）
                self.state = "OPEN"
                self.opened_at = time.time()
                self.half_open_inflight = 0
            elif self.consecutive_failures >= self.fail_threshold:
                self.state = "OPEN"
                self.opened_at = time.time()

    def reset(self) -> None:
        self.state = "CLOSED"
        self.consecutive_failures = 0
        self.opened_at = 0.0
        self.half_open_inflight = 0
        self.total_calls = 0
        self.total_failures = 0
        self.short_circuited = 0


def build_canonical(
    timestamp: str,
    method: str,
    path_with_query: str,
    body_bytes: bytes,
) -> str:
    """构造后端 InternalSignFilter 使用的规范串（已与后端逐字对齐）。

    canonical = f"{timestamp}\\n{METHOD}\\n{pathWithQuery}\\n{bodyUtf8}"

    四个必须严格一致的点（否则 401）：
    1. timestamp 与请求头 `X-Internal-Timestamp` **完全相同**的字符串（不要重新格式化、不补零）；
    2. METHOD 大写；
    3. pathWithQuery 是**服务端能看到的原始请求目标**，即
       `getRequestURI() + "?" + getQueryString()` —— 注意 `getQueryString()`
       返回的是 HTTP 请求行里**百分号编码后**的形式（如 `topic=%E7%BB%AD%E5%80%9F`），
       所以这里**必须是编码后的串**，不含协议/域名/端口；
    4. bodyUtf8 是请求体 UTF-8 字节直接解出的字符串；**无 body 时为空串**，
       但前面的 `\\n` 必须保留（canonical 以 `\\n` 结尾）。
    分隔符是单个 LF，共 3 个换行符、4 段。

    为什么用编码后的形式（而不是原始中文）：
    服务端根本没机会看到"未编码的请求行"——HTTP 报文里发出去的就是百分号编码的字节，
    HTTP 服务器只能把 `getQueryString()` 交给 Filter。这也是 AWS SigV4 等主流
    签名方案的做法（签名覆盖的是实际传输的规范化请求目标）。
    早期实现手工拼了"未编码"的 query 去做签名，结果是**只要 query 里带中文就必然 401**
    —— 典型的"只在非 ASCII 数据上才炸"的缺陷。
    """
    body_text = body_bytes.decode("utf-8") if body_bytes else ""
    return f"{timestamp}\n{method.upper()}\n{path_with_query}\n{body_text}"


def compute_sign(secret: str, canonical: str) -> str:
    """HMAC-SHA256 → **小写**十六进制（原始 32 字节的 hex，不做 base64）。"""
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_request(
    secret: str,
    method: str,
    path_with_query: str,
    body_bytes: bytes,
    timestamp: str,
) -> str:
    """计算 X-Internal-Sign。独立成函数便于后端同学对照与单测固定向量。"""
    return compute_sign(secret, build_canonical(timestamp, method, path_with_query, body_bytes))


def build_query_string(params: Mapping[str, Any] | None) -> str:
    """把查询参数拼成"未编码"的展示形式。

    ⚠️ **不要用它去算签名**。签名必须覆盖 httpx 实际发出的、百分号编码后的请求目标
    （服务端 `getQueryString()` 看到的形态），所以 `JavaClient.request` 是
    先 `client.build_request(...)` 再用 `request.url.raw_path` 取编码后的串。
    这个函数只用于日志/调试展示与单元测试里构造可读的期望值。
    """
    if not params:
        return ""
    pairs = [(k, v) for k, v in params.items() if v is not None]
    if not pairs:
        return ""
    # 展示用：保留原始字符，方便人和日志阅读
    return "?" + urlencode(pairs, doseq=True, quote_via=lambda s, *a: s)


def encoded_path_with_query(request: httpx.Request) -> str:
    """取出与 Java 侧 `getRequestURI() + "?" + getQueryString()` 等价的编码后请求目标。

    httpx 的 `url.raw_path` 正是"已百分号编码的 path + query"的 ASCII 字节，
    直接解码即可——**不要手工实现一套自己的编码规则**，否则会在
    `safe` 字符集、空格、`+`、中文编码等细节上与 httpx 产生偏差，
    而任何一处偏差都会让签名对不上（表现为随机的 401）。

    有些场景拿不到 `raw_path`（httpx 对极少数构造方式会省略），
    这时退回到 `url.path + "?" + url.query`——两者都是编码后的形式。
    """
    raw = getattr(request.url, "raw_path", None)
    if raw:
        return raw.decode("ascii")
    query = request.url.query.decode("ascii")
    return f"{request.url.path}?{query}" if query else request.url.path


def build_sign_headers(
    secret: str,
    method: str,
    path_with_query: str,
    body: bytes,
    *,
    timestamp: str | None = None,
) -> dict[str, str]:
    """参考实现（与后端规格一致）。

    注意 `path_with_query` 必须传**服务端能看到的编码后形式**
    （用 `encoded_path_with_query(request)` 取，或手写 `%E7%BB%AD%E5%80%9F` 这类编码串）。
    传未编码的中文会得到错误的签名——这正是历史上导致 401 的原因。
    """
    ts = timestamp or str(int(time.time()))
    return {
        "X-Internal-Timestamp": ts,
        "X-Internal-Sign": sign_request(secret, method, path_with_query, body, ts),
    }


class JavaClient:
    """对 Java 后端 /internal/agent/** 的异步客户端。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = (base_url or self.settings.java_base_url).rstrip("/")
        self.breaker = CircuitBreaker(
            fail_threshold=self.settings.java_circuit_fail_threshold,
            open_seconds=self.settings.java_circuit_open_seconds,
            half_open_probes=self.settings.java_circuit_half_open_probes,
        )
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # ---------------- 生命周期 ----------------
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # 连接池复用：一次对话可能连打 3~5 个后端接口，
            # 每次新建 TCP+TLS 握手会直接吃掉首 Token 延迟预算
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.settings.java_timeout_seconds),
                limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def __aenter__(self) -> "JavaClient":
        await self._get_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------- 核心请求 ----------------
    def _sign_headers(
        self,
        method: str,
        encoded_path_with_query: str,
        body_bytes: bytes,
        trace_id: str,
        *,
        timestamp: str | None = None,
    ) -> dict[str, str]:
        """生成签名相关的请求头。

        `encoded_path_with_query` 必须是**已百分号编码**的请求目标
        （用 `encoded_path_with_query(request)` 从构造好的请求里取）。
        """
        headers: dict[str, str] = {"X-Trace-Id": trace_id, "X-Agent-Caller": self.settings.app_name}
        if self.settings.internal_sign_enabled:
            ts = timestamp or str(int(time.time()))
            headers["X-Internal-Timestamp"] = ts
            headers["X-Internal-Sign"] = sign_request(
                self.settings.internal_sign_secret, method, encoded_path_with_query, body_bytes, ts
            )
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        trace_id: str,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        idempotent: bool = False,
    ) -> Any:
        """发起请求并解包统一响应体 `{code,msg,data,traceId}`。

        idempotent=True 表示该请求可安全重试（GET，或写操作但带了 idempotencyKey）。
        """
        if not await self.breaker.allow():
            raise CircuitOpenError(f"熔断器 {self.breaker.state}，跳过调用 {method} {path}")

        body_bytes = b""
        if json_body is not None:
            # 必须与真正发送的字节一致，否则后端算出来的签名对不上 → 401。
            # 因此手动序列化并直接用 content 发送，而不是交给 httpx 再序列化一次。
            body_bytes = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        request_params = {k: v for k, v in params.items() if v is not None} if params else None

        attempts = 1 + (self.settings.java_retries if (idempotent or method == "GET") else 0)
        last_error: Exception | None = None
        deadline = time.monotonic() + self.settings.java_timeout_seconds

        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                client = await self._get_client()
                # ---- 两阶段：先 build（拿到真实请求目标）→ 再签名 → 再 send ----
                # 为什么不能"先拼签名串再发"：签名必须覆盖 httpx 实际发出的编码后
                # 请求目标，而编码规则（safe 集合、中文 UTF-8 百分号编码等）属于
                # httpx 的实现细节。只有从构造好的请求对象上读 raw_path，
                # 才能保证"签的就是发的"。这是修掉 policy?topic=中文 必然 401 的关键。
                request = client.build_request(
                    method,
                    path,
                    params=request_params,
                    content=body_bytes if json_body is not None else None,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        # 后端 TraceIdFilter 读这个 header，并用 MDC 打到日志里
                        "X-Trace-Id": trace_id,
                        "X-Agent-Caller": self.settings.app_name,
                    },
                    timeout=httpx.Timeout(remaining),
                )
                for key, value in self._sign_headers(
                    method, encoded_path_with_query(request), body_bytes, trace_id
                ).items():
                    request.headers[key] = value
                resp = await client.send(request)
            except httpx.TimeoutException as exc:
                last_error = UpstreamTimeoutError(f"{method} {path} 超时: {exc}")
            except httpx.HTTPError as exc:
                last_error = UpstreamError(f"{method} {path} 网络错误: {exc}")
            else:
                if resp.status_code >= 500:
                    # 5xx 视为后端不健康：计入熔断，但仍按重试策略重试
                    last_error = UpstreamError(f"{method} {path} 返回 {resp.status_code}")
                elif resp.status_code >= 400:
                    # 4xx 是契约/权限问题，重试没有意义，直接失败
                    await self.breaker.record_failure()
                    raise UpstreamError(
                        f"{method} {path} 返回 {resp.status_code}",
                        user_message=f"图书馆系统拒绝了这次请求（{resp.status_code}），请联系馆员。",
                    )
                else:
                    await self.breaker.record_success()
                    return self._unwrap(resp, method, path)

            # 走到这里说明本次尝试失败，按指数退避 + 抖动重试
            if attempt < attempts - 1:
                backoff = min(0.2 * (2**attempt), 1.0) * (0.5 + random.random())
                remaining = deadline - time.monotonic()
                if remaining <= backoff:
                    break
                await asyncio.sleep(backoff)

        await self.breaker.record_failure()
        assert last_error is not None
        raise last_error

    @staticmethod
    def _unwrap(resp: httpx.Response, method: str, path: str) -> Any:
        """解包统一响应体；code != 0 时抛 UpstreamError 并把 reason 翻成中文。"""
        try:
            payload = resp.json()
        except ValueError as exc:
            raise UpstreamError(f"{method} {path} 返回非 JSON: {exc}") from exc

        if not isinstance(payload, dict):
            return payload
        code = payload.get("code", 0)
        if code in (0, None):
            return payload.get("data")
        reason = payload.get("msg") or payload.get("message") or ""
        raise UpstreamError(
            f"{method} {path} 业务失败 code={code} msg={reason}",
            user_message=friendly_reason(reason),
        )

    # ---------------- 8 个工具对应的后端接口（蓝图 §8.2）----------------
    async def search_books(
        self,
        *,
        query: str,
        category: str | None = None,
        page: int = 1,
        size: int = 10,
        sort_by: str = "relevance",
        trace_id: str,
    ) -> dict[str, Any]:
        """POST /internal/agent/tools/search-books"""
        body: dict[str, Any] = {"query": query, "page": page, "size": size, "sortBy": sort_by}
        if category:
            body["category"] = category
        data = await self.request(
            "POST",
            "/internal/agent/tools/search-books",
            trace_id=trace_id,
            json_body=body,
            idempotent=True,  # 检索是只读语义，重试安全
        )
        return data or {}

    async def get_stock(self, *, book_id: int, trace_id: str) -> dict[str, Any]:
        """GET /internal/agent/tools/books/{id}/stock"""
        data = await self.request(
            "GET",
            f"/internal/agent/tools/books/{book_id}/stock",
            trace_id=trace_id,
        )
        return data or {}

    async def borrow(
        self,
        *,
        reader_id: int,
        book_id: int,
        idempotency_key: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """POST /internal/agent/tools/borrow —— 写操作，靠 idempotencyKey 兜底幂等。"""
        return await self.request(
            "POST",
            "/internal/agent/tools/borrow",
            trace_id=trace_id,
            json_body={
                "readerId": reader_id,
                "bookId": book_id,
                "idempotencyKey": idempotency_key,
            },
            idempotent=True,  # 带幂等键，网络重试不会借两次
        ) or {}

    async def renew(
        self, *, reader_id: int, borrow_id: int, idempotency_key: str, trace_id: str
    ) -> dict[str, Any]:
        """POST /internal/agent/tools/renew"""
        return await self.request(
            "POST",
            "/internal/agent/tools/renew",
            trace_id=trace_id,
            json_body={
                "readerId": reader_id,
                "borrowId": borrow_id,
                "idempotencyKey": idempotency_key,
            },
            idempotent=True,
        ) or {}

    async def my_loans(self, *, reader_id: int, trace_id: str) -> list[dict[str, Any]]:
        """GET /internal/agent/tools/readers/{id}/loans"""
        data = await self.request(
            "GET",
            f"/internal/agent/tools/readers/{reader_id}/loans",
            trace_id=trace_id,
        )
        if isinstance(data, dict):
            # 后端若用分页包装，这里兼容取出 list
            data = data.get("list", [])
        return list(data or [])

    async def reader_profile(self, *, reader_id: int, trace_id: str) -> dict[str, Any]:
        """GET /internal/agent/tools/readers/{id}/profile"""
        data = await self.request(
            "GET",
            f"/internal/agent/tools/readers/{reader_id}/profile",
            trace_id=trace_id,
        )
        return data or {}

    async def create_reservation(
        self, *, reader_id: int, book_id: int, idempotency_key: str, trace_id: str
    ) -> dict[str, Any]:
        """POST /internal/agent/tools/reservations"""
        return await self.request(
            "POST",
            "/internal/agent/tools/reservations",
            trace_id=trace_id,
            json_body={
                "readerId": reader_id,
                "bookId": book_id,
                "idempotencyKey": idempotency_key,
            },
            idempotent=True,
        ) or {}

    async def policy(self, *, topic: str, trace_id: str) -> dict[str, Any]:
        """GET /internal/agent/tools/policy?topic=..."""
        data = await self.request(
            "GET",
            "/internal/agent/tools/policy",
            trace_id=trace_id,
            params={"topic": topic},
        )
        return data or {}

    # ---- recommend 场景依赖的两条内部桥接路径 ----
    # 蓝图 §8.2 只列了 8 条内部接口，没有单列"相似书/热门榜"。
    # 推荐工具需要它们，所以这里走 /internal/agent/tools/books/** 命名空间
    # （与 §8.2 同前缀、同签名、同限流隔离域），默认由后端桥接到公开的
    # /api/books/{id}/similar 与 /api/books/hot。若后端选择不暴露这两条，
    # 只需在 RecommendTool 中捕获 CircuitOpenError/UpstreamError 走热门榜兜底。
    async def similar_books(
        self, *, book_id: int, limit: int, trace_id: str
    ) -> list[dict[str, Any]]:
        data = await self.request(
            "GET",
            f"/internal/agent/tools/books/{book_id}/similar",
            trace_id=trace_id,
            params={"size": limit},
        )
        if isinstance(data, dict):
            data = data.get("list", [])
        return list(data or [])

    async def hot_books(self, *, limit: int, category: str | None, trace_id: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"size": limit}
        if category:
            params["category"] = category
        data = await self.request(
            "GET",
            "/internal/agent/tools/books/hot",
            trace_id=trace_id,
            params=params,
        )
        if isinstance(data, dict):
            data = data.get("list", [])
        return list(data or [])

    # ---------------- 供 /admin/metrics 观测 ----------------
    def health(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "timeout_seconds": self.settings.java_timeout_seconds,
            "retries": self.settings.java_retries,
            "sign_enabled": self.settings.internal_sign_enabled,
            "circuit": self.breaker.snapshot(),
        }


__all__ = [
    "JavaClient",
    "CircuitBreaker",
    "sign_request",
    "build_sign_headers",
    "build_canonical",
    "compute_sign",
    "build_query_string",
    "encoded_path_with_query",
    "SmartLibError",
]
