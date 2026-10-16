"""工具与健康检查接口。

GET /tools 暴露的 schema 与 LLM function calling 用的是同一份
（registry → Tool.json_schema），所以这个接口同时是"给前端看的文档"
和"给模型看的契约"——只有一个来源，不可能不一致。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.schemas import HealthResponse, ToolInfo, ToolsResponse
from app.rag.retriever import get_retriever
from app.tools import registry

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """健康检查。

    刻意把"Java 后端与熔断器状态"也带出来：Agent 自身活着不代表它能干活，
    读者体验取决于后端。K8s readiness 只应看进程存活，所以这里返回 200
    即使后端不可用，而把后端状态放在 body 里给运维看。
    """
    runtime = request.app.state.runtime
    java_client = request.app.state.java_client
    retriever = get_retriever(runtime.settings)
    return HealthResponse(
        status="ok",
        app=runtime.settings.app_name,
        version=runtime.settings.app_version,
        agent_mode=runtime.settings.agent_mode,
        llm_provider=getattr(runtime.llm, "provider", "none") or "none",
        java_backend=java_client.health() if java_client is not None else {"connected": False},
        knowledge_chunks=len(retriever.chunks) if retriever.ready else 0,
        tools=len(registry.all_tools()),
        trace_id=getattr(request.state, "trace_id", ""),
    )


@router.get("/tools", response_model=ToolsResponse)
async def list_tools(request: Request) -> ToolsResponse:
    """列出 8 个工具及其参数 schema。"""
    tools = [
        ToolInfo(
            name=tool.name,
            description=tool.description,
            is_write=tool.is_write,
            requires_reader=tool.requires_reader,
            provides_evidence=tool.provides_evidence,
            parameters=tool.args_model.model_json_schema(),
        )
        for tool in registry.all_tools()
    ]
    return ToolsResponse(
        trace_id=getattr(request.state, "trace_id", ""),
        count=len(tools),
        tools=tools,
    )


__all__ = ["router"]
