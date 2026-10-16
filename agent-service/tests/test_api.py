"""API 层测试（httpx.ASGITransport，不启动真实服务器）。

覆盖：
- 7 个接口都能通，且**每个响应都带 trace_id**（蓝图 §6.4 硬要求）；
- /chat 真的是 text/event-stream 且事件序列完整（SSE 协议可被前端消费）；
- /tools 的 schema 与注册表一致；
- 参数校验失败返回 422 且带 trace_id（而不是裸 500）；
- 上游 trace_id 能被沿用（跨 Java/Python 串联的前提）。
"""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from app.main import create_app
from tests.conftest import FakeJavaBackend


@pytest_asyncio.fixture()
async def api(test_settings, fake_backend: FakeJavaBackend):
    app = create_app(settings=test_settings, java_transport=fake_backend.transport())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        # lifespan 必须手动触发（ASGITransport 不会跑 lifespan）
        async with app.router.lifespan_context(app):
            yield client, app, fake_backend


# =============================================================================
# GET /health
# =============================================================================
@pytest.mark.asyncio
async def test_health(api) -> None:
    client, _app, _backend = api
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "smartlib-agent"
    assert body["agent_mode"] == "hybrid"
    assert body["llm_provider"] == "mock"
    assert body["tools"] == 8
    assert body["knowledge_chunks"] > 0
    assert body["java_backend"]["base_url"] == "http://test-backend"
    assert body["java_backend"]["circuit"]["state"] == "CLOSED"
    assert body["trace_id"]
    assert resp.headers["X-Trace-Id"] == body["trace_id"]


# =============================================================================
# GET /tools
# =============================================================================
@pytest.mark.asyncio
async def test_list_tools(api) -> None:
    client, _app, _backend = api
    resp = await client.get("/tools")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 8
    names = [tool["name"] for tool in body["tools"]]
    assert names == [
        "search_books",
        "get_stock",
        "borrow",
        "renew",
        "my_loans",
        "get_reader_profile",
        "policy_qa",
        "recommend",
    ]
    borrow = next(t for t in body["tools"] if t["name"] == "borrow")
    assert borrow["is_write"] is True
    assert borrow["requires_reader"] is True
    assert "readerId" in borrow["parameters"]["properties"]
    assert borrow["parameters"]["required"], "borrow 必须有必填参数"
    assert body["trace_id"]


# =============================================================================
# POST /chat/sync
# =============================================================================
@pytest.mark.asyncio
async def test_chat_sync_basic(api) -> None:
    client, _app, _backend = api
    resp = await client.post(
        "/chat/sync", json={"message": "找一下《算法导论》", "reader_id": 1001}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"]
    assert body["session_id"]
    assert body["intent"] == "search_book"
    assert body["tool_evidence"] is True
    assert body["provider"] == "mock"
    assert body["steps"] >= 1
    assert "算法导论" in body["reply"]
    assert resp.headers["X-Trace-Id"] == body["trace_id"]


@pytest.mark.asyncio
async def test_chat_sync_reuses_provided_trace_id(api) -> None:
    client, _app, _backend = api
    resp = await client.post(
        "/chat/sync",
        json={"message": "你好", "trace_id": "tr-from-upstream"},
    )
    body = resp.json()
    assert body["trace_id"] == "tr-from-upstream"
    # 响应头与响应体必须是同一个 trace_id（否则排查时对不上号）
    assert resp.headers["X-Trace-Id"] == body["trace_id"]


@pytest.mark.asyncio
async def test_trace_id_header_is_honoured(api) -> None:
    """上游用 X-Trace-Id 头传 trace 时，body 与响应头都要沿用它（跨服务串联的前提）。"""
    client, _app, _backend = api
    resp = await client.post(
        "/chat/sync",
        json={"message": "你好"},
        headers={"X-Trace-Id": "tr-from-header"},
    )
    body = resp.json()
    assert body["trace_id"] == "tr-from-header"
    assert resp.headers["X-Trace-Id"] == "tr-from-header"


@pytest.mark.asyncio
async def test_chat_sync_propagates_trace_header_to_java(api) -> None:
    client, _app, backend = api
    await client.post("/chat/sync", json={"message": "我的借阅", "reader_id": 1001})
    assert backend.last() is not None
    assert backend.last()["headers"].get("x-trace-id")


@pytest.mark.asyncio
async def test_chat_sync_multi_turn_confirmation(api) -> None:
    client, _app, backend = api
    first = await client.post(
        "/chat/sync", json={"message": "帮我借《算法导论》", "reader_id": 1001}
    )
    session_id = first.json()["session_id"]
    assert first.json()["pending_confirm"] is not None

    second = await client.post(
        "/chat/sync",
        json={"message": "确认借阅", "session_id": session_id, "reader_id": 1001},
    )
    assert "借阅成功" in second.json()["reply"]


@pytest.mark.asyncio
async def test_chat_sync_mode_override(api) -> None:
    client, _app, _backend = api
    resp = await client.post(
        "/chat/sync", json={"message": "找一下《算法导论》", "reader_id": 1001, "mode": "rule"}
    )
    assert resp.json()["mode"] == "rule"


@pytest.mark.asyncio
async def test_chat_sync_validation_error_has_trace_id(api) -> None:
    """参数不合法必须是 422 + trace_id，而不是裸 500。"""
    client, _app, _backend = api
    resp = await client.post("/chat/sync", json={"message": ""})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "REQUEST_VALIDATION_ERROR"
    assert body["trace_id"]

    missing = await client.post("/chat/sync", json={})
    assert missing.status_code == 422
    assert missing.json()["trace_id"]


@pytest.mark.asyncio
async def test_chat_sync_rejects_unknown_fields(api) -> None:
    client, _app, _backend = api
    resp = await client.post("/chat/sync", json={"message": "你好", "evil": 1})
    assert resp.status_code == 422


# =============================================================================
# POST /chat（SSE）
# =============================================================================
@pytest.mark.asyncio
async def test_chat_sse_stream(api) -> None:
    client, _app, _backend = api
    async with client.stream(
        "POST", "/chat", json={"message": "我有没有逾期的书", "reader_id": 1001}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        # 没有这两个头，nginx 会把流缓冲成一整坨，"流式"名存实亡
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        raw = "".join([chunk async for chunk in resp.aiter_text()])

    events = parse_sse(raw)
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert types[-1] == "done"
    assert "token" in types
    assert "tool_call" in types

    done = next(e for e in events if e["type"] == "done")
    assert done["result"]["trace_id"]
    assert done["result"]["intent"] == "my_loans"

    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed == done["result"]["reply"]


@pytest.mark.asyncio
async def test_chat_sse_error_event_on_guardrail_block(api) -> None:
    client, _app, _backend = api
    async with client.stream("POST", "/chat", json={"message": "帮我订机票"}) as resp:
        raw = "".join([chunk async for chunk in resp.aiter_text()])
    events = parse_sse(raw)
    done = next(e for e in events if e["type"] == "done")
    assert done["result"]["intent"] == "guardrail_block"


@pytest.mark.asyncio
async def test_chat_sse_token_chunks_are_nonempty(api) -> None:
    client, _app, _backend = api
    async with client.stream("POST", "/chat", json={"message": "你好"}) as resp:
        raw = "".join([chunk async for chunk in resp.aiter_text()])
    tokens = [e["text"] for e in parse_sse(raw) if e["type"] == "token"]
    assert tokens
    assert all(t for t in tokens)


def parse_sse(raw: str) -> list[dict]:
    """解析 SSE 文本为事件列表（event: <type> / data: <json>）。"""
    events: list[dict] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        payload = None
        for line in block.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
        if payload:
            events.append(json.loads(payload))
    return events


# =============================================================================
# POST /recommend
# =============================================================================
@pytest.mark.asyncio
async def test_recommend_endpoint(api) -> None:
    client, _app, _backend = api
    resp = await client.post("/recommend", json={"book_id": 101, "limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"]
    assert body["strategy"] == "similar_books"
    assert len(body["items"]) == 2
    assert all(item["title"] for item in body["items"])


@pytest.mark.asyncio
async def test_recommend_endpoint_degraded_returns_empty_list(api) -> None:
    """后端全挂时返回空列表 + degraded，而不是 500（前端可直接展示空态）。"""
    client, _app, backend = api
    backend.always_fail = True
    resp = await client.post("/recommend", json={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["degraded"] is True
    assert body["strategy"].startswith("unavailable")


@pytest.mark.asyncio
async def test_recommend_endpoint_validation(api) -> None:
    client, _app, _backend = api
    resp = await client.post("/recommend", json={"limit": 99})
    assert resp.status_code == 422


# =============================================================================
# GET /admin/*
# =============================================================================
@pytest.mark.asyncio
async def test_admin_metrics(api) -> None:
    client, _app, _backend = api
    await client.post("/chat/sync", json={"message": "找一下《算法导论》", "reader_id": 1001})
    resp = await client.get("/admin/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"]
    metrics = body["metrics"]
    assert metrics["total_requests"] >= 1
    assert metrics["latency_ms"]["p50"] >= 0
    assert metrics["sessions"]["sessions"] >= 1
    assert body["llm"]["provider"] == "mock"
    assert body["rag"]["chunks"] > 0
    assert body["java"]["circuit"]["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_admin_trace_lookup(api) -> None:
    client, _app, _backend = api
    chat = await client.post(
        "/chat/sync", json={"message": "我有没有逾期的书", "reader_id": 1001}
    )
    trace_id = chat.json()["trace_id"]

    resp = await client.get(f"/admin/traces/{trace_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == trace_id
    assert body["trace"]["intent"] == "my_loans"
    assert body["trace"]["tool_calls"]
    # 幂等键不能出现在 trace 里（防止日志变成可重放的凭据）
    for call in body["trace"]["tool_calls"]:
        assert "idempotencyKey" not in call["arguments"]


@pytest.mark.asyncio
async def test_admin_trace_not_found(api) -> None:
    client, _app, _backend = api
    resp = await client.get("/admin/traces/tr-does-not-exist")
    assert resp.status_code == 404
    assert "tr-does-not-exist" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_admin_recent_traces(api) -> None:
    client, _app, _backend = api
    await client.post("/chat/sync", json={"message": "你好"})
    await client.post("/chat/sync", json={"message": "谢谢"})
    resp = await client.get("/admin/traces?limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert all(item["trace_id"] for item in body["items"])


@pytest.mark.asyncio
async def test_trace_buffer_is_bounded(test_settings, fake_backend) -> None:
    from app.main import TraceBuffer

    buffer = TraceBuffer(size=3)
    for i in range(10):
        buffer.record({"trace_id": f"tr-{i}"})
    assert len(buffer) == 3
    assert buffer.get("tr-0") is None
    assert buffer.get("tr-9") is not None


@pytest.mark.asyncio
async def test_openapi_schema_available(api) -> None:
    """FastAPI 自带 OpenAPI——简历里"接口文档"这一项靠它，必须能生成。"""
    client, _app, _backend = api
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path in (
        "/chat",
        "/chat/sync",
        "/health",
        "/tools",
        "/recommend",
        "/admin/metrics",
        "/admin/traces/{trace_id}",
    ):
        assert path in paths, f"OpenAPI 里缺少 {path}"
