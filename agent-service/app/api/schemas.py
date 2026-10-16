"""API 层的请求/响应模型。

统一约定：**所有响应都带 trace_id**（蓝图 §6.4 全链路追踪硬要求），
包括错误响应，这样读者反馈"我这边报错了"时能直接按 trace_id 捞出后端日志。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    """对话请求。既支持一次性问答，也支持同一会话多轮（靠 session_id 串联记忆）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message: str = Field(..., min_length=1, max_length=1000, description="读者输入的自然语言")
    session_id: str | None = Field(
        None, max_length=64, description="会话 ID，不传则服务端生成（用于多轮记忆与写操作确认）"
    )
    reader_id: int | None = Field(None, ge=1, description="读者 ID，涉及个人数据的工具必填")
    mode: Literal["rule", "llm", "hybrid", "plan"] | None = Field(
        None, description="覆盖默认 AGENT_MODE，便于对比 G1~G5 方案效果"
    )
    trace_id: str | None = Field(None, max_length=64, description="上游已有 trace 时透传")


class ToolCallTrace(BaseModel):
    """一次工具调用的可观测记录（写进 trace，也在 SSE 里推给前端做过程展示）。"""

    step: int
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = False
    latency_ms: float = 0.0
    error_code: str | None = None
    summary: str = ""
    blocked_by: str | None = None


class ChatResponse(BaseModel):
    trace_id: str
    session_id: str
    reply: str
    intent: str = ""
    intent_confidence: float = 0.0
    mode: str = "hybrid"
    provider: str = "mock"
    steps: int = 0
    tool_evidence: bool = False
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    rag_sources: list[str] = Field(default_factory=list)
    handoff: bool = False
    latency_ms: float = 0.0
    guardrail_notes: list[str] = Field(default_factory=list)
    degraded: bool = False
    #: 待用户二次确认的写操作（非空表示"还没执行，等你确认"）
    pending_confirm: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None


class RecommendRequest(BaseModel):
    """独立的推荐接口（蓝图 §8.1 之外由 Agent 服务提供的便捷入口）。"""

    model_config = ConfigDict(extra="forbid")

    reader_id: int | None = Field(None, ge=1)
    book_id: int | None = Field(None, ge=1, description="以某本书为种子")
    category: str | None = Field(None, max_length=40)
    limit: int = Field(3, ge=1, le=10)
    trace_id: str | None = None


class RecommendItem(BaseModel):
    book_id: int | None = None
    title: str
    author: str | None = None
    category: str | None = None
    reason: str = ""
    available: int | None = None


class RecommendResponse(BaseModel):
    trace_id: str
    items: list[RecommendItem] = Field(default_factory=list)
    strategy: str = ""
    degraded: bool = False


class ToolInfo(BaseModel):
    name: str
    description: str
    is_write: bool
    requires_reader: bool
    provides_evidence: bool
    parameters: dict[str, Any]


class ToolsResponse(BaseModel):
    trace_id: str
    count: int
    tools: list[ToolInfo]


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    agent_mode: str
    llm_provider: str
    java_backend: dict[str, Any]
    knowledge_chunks: int
    tools: int
    trace_id: str


class MetricsResponse(BaseModel):
    trace_id: str
    metrics: dict[str, Any]
    llm: dict[str, Any]
    java: dict[str, Any]
    rag: dict[str, Any]


class ErrorResponse(BaseModel):
    trace_id: str
    code: str
    message: str


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ToolCallTrace",
    "RecommendRequest",
    "RecommendResponse",
    "RecommendItem",
    "ToolInfo",
    "ToolsResponse",
    "HealthResponse",
    "MetricsResponse",
    "ErrorResponse",
]
