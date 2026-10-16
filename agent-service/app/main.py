"""FastAPI 入口：路由装配、生命周期、trace 中间件。

启动顺序（lifespan）：
1. 建 Settings → 建 JavaClient（不发起连接，惰性建连）
2. 建 LLMRouter（按 LLM_PROVIDER 选 mock / openai_compat，并挂降级）
3. **预构建 RAG 索引**：知识库只有几百个 chunk，启动时建好可以让首个
   规则问题直接命中索引，否则第一次问"能借几本"要等建索引（几十毫秒到百毫秒）。
4. 装配 AgentRuntime 并挂到 app.state

生命周期退出时关闭 httpx 连接池——不关的话 uvicorn 重载时会漏 socket。

测试可注入点：`create_app(java_transport=...)`。
pytest 用 httpx.MockTransport 注入，因此**不需要启动真实 Java 后端**。
"""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent.guardrail import Guardrail
from app.agent.intent import IntentClassifier
from app.agent.planner import Planner
from app.agent.runtime import AgentRuntime
from app.api import admin, chat, recommend, tools
from app.client.java_client import JavaClient
from app.config import Settings, get_settings
from app.llm.router import LLMRouter
from app.rag.retriever import Retriever, get_retriever
from app.tools import registry

logger = logging.getLogger("smartlib.agent")

#: 内存里保留多少条 trace（见 api/admin.py 的说明）
TRACE_BUFFER_SIZE = 500


class TraceBuffer:
    """有界 trace 环形缓冲。"""

    def __init__(self, size: int = TRACE_BUFFER_SIZE) -> None:
        self.size = size
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def record(self, payload: dict[str, Any]) -> None:
        trace_id = payload.get("trace_id")
        if not trace_id:
            return
        self._items[trace_id] = payload
        self._items.move_to_end(trace_id)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def get(self, trace_id: str) -> dict[str, Any] | None:
        return self._items.get(trace_id)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        items = list(self._items.values())
        return list(reversed(items))[: max(1, min(limit, 200))]

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        self._items.clear()


def create_app(
    *,
    settings: Settings | None = None,
    java_transport: httpx.AsyncBaseTransport | None = None,
    llm: Any = None,
) -> FastAPI:
    """构造 FastAPI 应用。参数都是为测试注入准备的。"""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        java_client = JavaClient(cfg, transport=java_transport)
        router_llm = llm if llm is not None else LLMRouter(cfg)
        primary = llm if llm is not None else router_llm.primary

        retriever: Retriever = get_retriever(cfg)
        if not retriever.ready:
            await retriever.build()

        guardrail = Guardrail(cfg)
        app.state.settings = cfg
        app.state.java_client = java_client
        app.state.llm_router = router_llm
        app.state.retriever = retriever
        app.state.traces = TraceBuffer()
        app.state.runtime = AgentRuntime(
            settings=cfg,
            llm=primary,
            java_client=java_client,
            guardrail=guardrail,
            classifier=IntentClassifier(primary, cfg),
            planner=Planner(primary, cfg),
        )
        app.state.record_trace = app.state.traces.record
        app.state.get_trace = app.state.traces.get
        app.state.recent_traces = app.state.traces.recent

        logger.info(
            "SmartLib Agent 启动完成：mode=%s provider=%s tools=%d knowledge_chunks=%d",
            cfg.agent_mode,
            getattr(primary, "provider", "none"),
            len(registry.all_tools()),
            len(retriever.chunks),
        )
        try:
            yield
        finally:
            await java_client.aclose()
            if hasattr(router_llm, "aclose"):
                await router_llm.aclose()

    app = FastAPI(
        title="SmartLib Agent Service",
        version=cfg.app_version,
        description=(
            "高校图书馆智能借阅助手的 Agent 服务（FastAPI）。\n"
            "默认 LLM_PROVIDER=mock：零 API Key、零外部依赖即可完整运行与评测。"
        ),
        lifespan=lifespan,
    )

    # ---------------- trace 中间件 ----------------
    @app.middleware("http")
    async def trace_middleware(request: Request, call_next: Any) -> Any:
        """全链路 trace_id：沿用上游传入的，没有就生成。

        为什么不用第三方中间件：我们需要 trace_id 同时出现在
        (a) 响应头、(b) request.state（供各 handler 使用）、
        (c) 错误响应体里。只有一处实现才不会漏。

        回写响应头时**重新读一次 request.state.trace_id**：
        chat 路由允许请求体里显式指定 trace_id（会更新 state），
        如果这里用进入时捕获的旧值，就会出现"响应头一个 id、body 里另一个 id"。
        """
        incoming = request.headers.get("X-Trace-Id")
        request.state.trace_id = incoming or f"tr-{uuid.uuid4().hex[:16]}"
        response = await call_next(request)
        response.headers["X-Trace-Id"] = getattr(request.state, "trace_id", None) or incoming or ""
        return response

    # ---------------- 异常处理：错误也带 trace_id ----------------
    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "trace_id": getattr(request.state, "trace_id", ""),
                "code": "REQUEST_VALIDATION_ERROR",
                "message": "请求参数不合法",
                "detail": exc.errors()[:5],
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 trace_id=%s", getattr(request.state, "trace_id", ""))
        return JSONResponse(
            status_code=500,
            content={
                "trace_id": getattr(request.state, "trace_id", ""),
                "code": "INTERNAL_ERROR",
                # 不把异常详情返给用户（可能含内部结构），只回 trace_id 供排查
                "message": "服务内部错误，请稍后再试。",
            },
        )

    app.include_router(chat.router)
    app.include_router(tools.router)
    app.include_router(recommend.router)
    app.include_router(admin.router)

    # ---------------- 跨域：只给本地演示控制台开口子 ----------------
    #
    # 背景：演示控制台（backend 的静态页 /demo.html）与 Agent 不同源，
    # 浏览器会先发 OPTIONS 预检；没有 CORS 时演示第一步就崩。
    #
    # 两个容易踩的点：
    #   1. Starlette 的 add_middleware 是**插到最前面**（最外层），
    #      所以这段写在 trace 中间件之后也仍然是外层 ——
    #      预检请求由 CORS 直接应答，不会进入业务中间件，这是期望行为。
    #   2. 白名单来自配置（默认只放本机），不是 "*"：
    #      allow_origins=["*"] 会让任何网页都能调用这个能替读者借书的服务。
    # expose_headers 让浏览器里的 JS 能读到 X-Trace-Id，演示时可以直接展示链路 ID。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Content-Type", "X-Trace-Id", "X-Internal-Timestamp", "X-Internal-Sign"],
        expose_headers=["X-Trace-Id"],
    )
    logger.info("CORS 白名单：%s", cfg.cors_origin_list)
    return app


#: uvicorn 入口：`uvicorn app.main:app`
app = create_app()


def main() -> None:
    """`python -m app.main` 直接起服务（等价于 uvicorn 命令）。"""
    import uvicorn

    cfg = get_settings()
    uvicorn.run(
        "app.main:app",
        host=cfg.host,
        port=cfg.port,
        log_level=cfg.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["app", "create_app", "TraceBuffer", "main"]
