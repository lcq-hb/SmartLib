"""测试公共设施：伪造 Java 后端 + 参考签名实现。

为什么要"参考实现"而不是复用业务代码里的签名函数：
如果测试直接调用 `java_client.sign_request`，那么签名逻辑写错了测试也会跟着错。
测试里独立实现一遍（照后端 InternalSignFilter 的规格），
断言两者输出一致，才真正验证了协议——这是"固定向量"测试的意义。

所有测试都通过 httpx.MockTransport 打到这里的 FakeJavaBackend，
**没有一个测试需要启动真实 Java 后端**。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable
from urllib.parse import unquote

import httpx
import pytest

from app.config import Settings, get_settings

DEFAULT_SECRET = "dev-internal-secret-please-change"

# =============================================================================
# 后端签名规格的独立参考实现
# =============================================================================
def reference_sign(secret: str, timestamp: str, method: str, path_with_query: str, body: bytes) -> str:
    """照后端规格独立实现一遍：

    canonical = timestamp + "\\n" + METHOD + "\\n" + pathWithQuery + "\\n" + bodyUtf8
    sign      = hex_lowercase(HMAC_SHA256(secret, canonical))
    """
    body_text = body.decode("utf-8") if body else ""
    canonical = f"{timestamp}\n{method.upper()}\n{path_with_query}\n{body_text}"
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


# =============================================================================
# 假后端
# =============================================================================
class FakeJavaBackend:
    """内存版 Java 后端，行为对齐蓝图 §8.2 的 8 条内部接口。

    支持故障注入（fail_times / always_fail）来测重试与熔断。
    """

    def __init__(self, *, secret: str = DEFAULT_SECRET, strict_sign: bool = True) -> None:
        self.secret = secret
        self.strict_sign = strict_sign
        self.calls: list[dict[str, Any]] = []
        self.fail_times = 0
        self.always_fail = False
        self.status_code_times: dict[int, int] = {}
        #: 借阅/续借的模拟结果，测试可以直接改写
        self.borrow_ok = True
        self.borrow_reason = ""
        self.renew_ok = True
        self.renew_reason = ""
        self.stock_available: int | None = 3
        self.loan_items: list[dict[str, Any]] | None = None
        self.profile: dict[str, Any] | None = None

    # ---------------- 默认数据 ----------------
    def default_loans(self) -> list[dict[str, Any]]:
        return [
            {
                "borrowId": 5001,
                "bookId": 101,
                "title": "算法导论",
                "dueDate": "2026-03-01",
                "renewCount": 0,
                "renewable": True,
                "overdueDays": 0,
            },
            {
                "borrowId": 5002,
                "bookId": 102,
                "title": "计算机网络",
                "dueDate": "2026-01-05",
                "renewCount": 2,
                "renewable": False,
                "overdueDays": 12,
            },
        ]

    def default_profile(self) -> dict[str, Any]:
        return {
            "readerType": "UNDERGRAD",
            "creditScore": 92,
            "preferredCategories": ["计算机", "数学"],
            "borrowedCount": 2,
        }

    def default_search(self, query: str) -> dict[str, Any]:
        return {
            "items": [
                {
                    "bookId": 101,
                    "title": "算法导论",
                    "author": "Thomas H. Cormen",
                    "publisher": "机械工业出版社",
                    "publishYear": 2013,
                    "category": "计算机",
                    "availableCopies": 3,
                    "totalCopies": 5,
                    "shelfLocation": "3 楼中文科技图书区 TP301.6/C822",
                },
                {
                    "bookId": 106,
                    "title": "数据结构与算法分析",
                    "author": "Mark Allen Weiss",
                    "publisher": "机械工业出版社",
                    "publishYear": 2016,
                    "category": "计算机",
                    "availableCopies": 0,
                    "totalCopies": 4,
                    "shelfLocation": "3 楼中文科技图书区 TP311.12/W429",
                },
            ],
            "total": 2,
            "tookMs": 18,
            "engine": "inverted-index",
            "degraded": False,
        }

    # ---------------- 路由 ----------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # httpx 的 url.query 是**已百分号编码**的原始字节（中文会被编码）。
        # 后端规格里的 pathWithQuery 是未编码形式，所以这里必须先 unquote 再验签，
        # 否则中文 query 会因为"%E7%BB..."与"续借"不一致而误判 401。
        raw_query = request.url.query.decode("utf-8") if request.url.query else ""
        path_with_query = f"{path}?{unquote(raw_query)}" if raw_query else path
        body = request.content or b""

        self.calls.append(
            {
                "method": request.method,
                "path": path,
                "query": raw_query,
                "body": body,
                "headers": dict(request.headers),
            }
        )

        if self.strict_sign:
            problem = self._verify_sign(request, path_with_query, body)
            if problem:
                return self._envelope({"code": 401, "msg": "SIGN_INVALID"}, status=401)

        if self.always_fail:
            return httpx.Response(503, json={"code": 503, "msg": "SERVICE_UNAVAILABLE"})
        if self.fail_times > 0:
            self.fail_times -= 1
            return httpx.Response(503, json={"code": 503, "msg": "SERVICE_UNAVAILABLE"})
        for status, times in list(self.status_code_times.items()):
            if times > 0:
                self.status_code_times[status] = times - 1
                return httpx.Response(status, json={"code": status, "msg": "INJECTED"})

        return self._route(request, path, raw_query, body)

    def _verify_sign(self, request: httpx.Request, path_with_query: str, body: bytes) -> str | None:
        ts = request.headers.get("X-Internal-Timestamp")
        sign = request.headers.get("X-Internal-Sign")
        if not ts or not sign:
            return "缺少签名头"
        expected = reference_sign(self.secret, ts, request.method, path_with_query, body)
        if not hmac.compare_digest(expected, sign):
            return "签名不匹配"
        if len(sign) != 64 or sign != sign.lower():
            return "签名必须是 64 位小写 hex"
        return None

    @staticmethod
    def _envelope(data: Any, *, status: int = 200, msg: str = "ok") -> httpx.Response:
        return httpx.Response(status, json={"code": 0, "msg": msg, "data": data, "traceId": "tr-backend"})

    def _route(
        self, request: httpx.Request, path: str, raw_query: str, body: bytes
    ) -> httpx.Response:
        payload: dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except ValueError:
                payload = {}

        if path == "/internal/agent/tools/search-books":
            return self._envelope(self.default_search(str(payload.get("query") or "")))

        if path.endswith("/stock") and "/books/" in path:
            available = self.stock_available
            return self._envelope(
                {
                    "bookId": 101,
                    "title": "算法导论",
                    "available": available,
                    "total": 5,
                    "queueLen": 0 if (available or 0) > 0 else 3,
                    "shelfLocation": "3 楼中文科技图书区 TP301.6/C822",
                    "degraded": available is not None and available < 0,
                }
            )

        if path == "/internal/agent/tools/borrow":
            return self._envelope(
                {
                    "ok": self.borrow_ok,
                    "borrowId": 9001 if self.borrow_ok else None,
                    "dueDate": "2026-04-01" if self.borrow_ok else None,
                    "reason": self.borrow_reason,
                }
            )

        if path == "/internal/agent/tools/renew":
            return self._envelope(
                {
                    "ok": self.renew_ok,
                    "newDueDate": "2026-05-01" if self.renew_ok else None,
                    "reason": self.renew_reason,
                }
            )

        if path.endswith("/loans") and "/readers/" in path:
            items = self.loan_items if self.loan_items is not None else self.default_loans()
            return self._envelope(items)

        if path.endswith("/profile") and "/readers/" in path:
            return self._envelope(self.profile if self.profile is not None else self.default_profile())

        if path == "/internal/agent/tools/reservations":
            return self._envelope({"ok": True, "queueNo": 4, "reason": ""})

        if path == "/internal/agent/tools/policy":
            topic = ""
            for pair in raw_query.split("&"):
                if pair.startswith("topic="):
                    # 同样要先 unquote：后端拿到的是解码后的中文 query 参数
                    topic = unquote(pair[len("topic=") :])
            return self._envelope(
                {
                    "answer": f"（规则库直答）关于「{topic}」：本科生可借 10 册，借期 30 天。",
                    "source": "rule_config:borrow.limit.UNDERGRAD",
                }
            )

        if path.endswith("/similar") and "/books/" in path:
            return self._envelope(
                [
                    {"bookId": 106, "title": "数据结构与算法分析", "author": "Weiss", "category": "计算机"},
                    {"bookId": 104, "title": "深入理解计算机系统", "author": "Bryant", "category": "计算机"},
                ]
            )

        if path == "/internal/agent/tools/books/hot":
            return self._envelope(
                [
                    {"bookId": 103, "title": "高等数学", "author": "同济大学", "category": "数学"},
                    {"bookId": 109, "title": "机器学习", "author": "周志华", "category": "计算机"},
                ]
            )

        return self._envelope(None, status=404, msg=f"未实现的假接口: {path}")

    # ---------------- 便捷断言 ----------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]

    def last(self) -> dict[str, Any] | None:
        return self.calls[-1] if self.calls else None

    def clear(self) -> None:
        self.calls.clear()


# =============================================================================
# fixtures
# =============================================================================
@pytest.fixture()
def fake_backend() -> FakeJavaBackend:
    return FakeJavaBackend()


@pytest.fixture()
def test_settings() -> Settings:
    """隔离的测试配置：不读 .env，知识库目录由 config 按包路径反推。

    刻意**不用 tmp_path**：本机沙箱下 pytest 的临时目录不可写，
    而我们的测试本来就不需要落盘（假后端在内存里、知识库是只读的），
    去掉它反而让用例在任何环境都能跑。
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        llm_provider="mock",
        agent_mode="hybrid",
        java_base_url="http://test-backend",
        internal_sign_secret=DEFAULT_SECRET,
        log_level="WARNING",
    )


@pytest.fixture()
def java_client(test_settings: Settings, fake_backend: FakeJavaBackend) -> Any:
    from app.client.java_client import JavaClient

    return JavaClient(test_settings, transport=fake_backend.transport())


@pytest.fixture()
def runtime_env(test_settings: Settings, fake_backend: FakeJavaBackend) -> Any:
    """一套完整的运行时环境（MockLLM + 假后端），供 agent/guardrail 用例复用。"""
    from app.agent.guardrail import Guardrail
    from app.agent.intent import IntentClassifier
    from app.agent.planner import Planner
    from app.agent.runtime import AgentRuntime
    from app.client.java_client import JavaClient
    from app.llm.mock import MockLLM

    llm = MockLLM(test_settings)
    client = JavaClient(test_settings, transport=fake_backend.transport())
    return AgentRuntime(
        settings=test_settings,
        llm=llm,
        java_client=client,
        guardrail=Guardrail(test_settings),
        classifier=IntentClassifier(llm, test_settings),
        planner=Planner(llm, test_settings),
    )


def reset_singletons() -> None:
    """清掉进程级单例（会话/画像），避免用例之间互相污染。"""
    from app.memory.reader_profile import reset_profile_store
    from app.memory.session_store import reset_session_store

    reset_session_store()
    reset_profile_store()


@pytest.fixture(autouse=True)
def _clean_singletons() -> Any:
    reset_singletons()
    yield
    reset_singletons()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make_app(fake_backend: FakeJavaBackend, settings: Settings) -> Any:
    """构造注入了假后端的 FastAPI 应用（供 API 用例使用）。"""
    from app.main import create_app

    return create_app(settings=settings, java_transport=fake_backend.transport())


__all__ = [
    "FakeJavaBackend",
    "reference_sign",
    "make_app",
    "reset_singletons",
    "DEFAULT_SECRET",
]
