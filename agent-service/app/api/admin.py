"""运维接口：指标与链路详情。

GET /admin/metrics           —— 进程内滚动窗口指标（对齐蓝图 §7 的 SLO）
GET /admin/traces/{trace_id} —— 按 trace_id 捞出一次对话的完整过程

trace 存储用**有界环形缓冲**（默认最近 500 条）而不是全量：
演示与排障只需要"最近发生了什么"，全量存会随运行时间无限涨内存。
生产环境应当把 trace 打到日志/APM，这里的内存实现是零依赖的等价替代。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import MetricsResponse
from app.memory.reader_profile import get_profile_store
from app.memory.session_store import get_session_store
from app.metrics import METRICS
from app.rag.retriever import get_retriever

router = APIRouter(tags=["admin"])


@router.get("/admin/metrics", response_model=MetricsResponse)
async def metrics(request: Request) -> MetricsResponse:
    runtime = request.app.state.runtime
    java_client = request.app.state.java_client
    retriever = get_retriever(runtime.settings)

    llm_view: dict[str, Any] = {
        "provider": getattr(runtime.llm, "provider", "none"),
        "model": runtime.settings.llm_model if runtime.settings.use_real_llm else "mock",
    }
    if runtime.llm_router is not None:
        llm_view.update(runtime.llm_router.snapshot())

    return MetricsResponse(
        trace_id=getattr(request.state, "trace_id", ""),
        metrics={
            **METRICS.snapshot(),
            "sessions": get_session_store(runtime.settings).stats(),
            "profiles": get_profile_store(runtime.settings).stats(),
            "traces_buffered": len(request.app.state.traces),
        },
        llm=llm_view,
        java=java_client.health() if java_client is not None else {},
        rag=retriever.stats,
    )


@router.get("/admin/traces/{trace_id}")
async def get_trace(trace_id: str, request: Request) -> dict[str, Any]:
    record = request.app.state.get_trace(trace_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"没有找到 trace {trace_id}（仅保留最近 500 条）",
        )
    return {"trace_id": trace_id, "trace": record}


@router.get("/admin/traces")
async def list_traces(request: Request, limit: int = 20) -> dict[str, Any]:
    """最近若干条 trace 的概要，便于快速定位"刚才那次为什么答错了"。"""
    records = request.app.state.recent_traces(limit)
    return {
        "trace_id": getattr(request.state, "trace_id", ""),
        "count": len(records),
        "items": [
            {
                "trace_id": item.get("trace_id"),
                "intent": item.get("intent"),
                "mode": item.get("mode"),
                "steps": item.get("steps"),
                "tool_evidence": item.get("tool_evidence"),
                "latency_ms": item.get("latency_ms"),
                "reply": (item.get("reply") or "")[:60],
            }
            for item in records
        ],
    }


__all__ = ["router"]
