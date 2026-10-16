"""工具层基础设施：调用上下文、统一返回、工具基类。

设计决策：**工具外层参数名保持 camelCase（readerId/bookId）**，与蓝图 §8.2 的
JSON 契约逐字一致。理由：LLM 的 function calling 参数、/tools 接口暴露的 schema、
Java 后端的入参三处同名，评审时可以直接对照，不需要在脑子里做命名转换。
Python 内部的局部变量仍用 snake_case（见各工具实现）。

一次工具调用的完整生命周期：
    registry 取 schema → guardrail 校验参数 → Pydantic 强校验 →
    Tool.handle() 调 Java 后端 → 失败则 raise ToolError → executor 捕获并转成
    给用户看的 ToolResult(ok=False)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel

from app.client.java_client import JavaClient
from app.config import Settings, get_settings


@dataclass
class ToolContext:
    """工具执行上下文：一次对话内所有工具共享。"""

    trace_id: str
    reader_id: int | None = None
    session_id: str = ""
    java: JavaClient | None = None
    settings: Settings = field(default_factory=get_settings)
    #: 透传给工具的额外信息：plan 步骤序号、二次确认标记、去重用的已调用签名等
    meta: dict[str, Any] = field(default_factory=dict)

    def require_reader(self) -> int:
        """需要读者身份的工具调用它；没有身份时给一句明确的追问，而不是报错。"""
        if self.reader_id is None:
            raise ToolError(
                "缺少读者身份",
                user_message="我还不知道您是哪位读者，请先提供您的读者编号（例如 readerId=1001）。",
                code="READER_REQUIRED",
            )
        return self.reader_id


class ToolError(Exception):
    """工具可预期的业务失败。

    与"系统异常"区分开：这类失败要原样把原因讲给用户听（书没了、信用分不够），
    而不是走"系统繁忙"话术。
    """

    def __init__(self, message: str, *, user_message: str = "", code: str = "TOOL_FAILED") -> None:
        super().__init__(message)
        self.detail = message
        self.user_message = user_message or message
        self.code = code


@dataclass
class ToolResult:
    """工具执行结果。data 会被塞回 LLM 当观察结果，summary 用于快速渲染与评测断言。"""

    ok: bool
    tool: str
    data: Any = None
    summary: str = ""
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: float = 0.0

    def to_observation(self) -> dict[str, Any]:
        """转成喂给 LLM 的观察结果（保持结构化，不用自然语言，避免二次幻觉）。"""
        payload: dict[str, Any] = {"tool": self.tool, "ok": self.ok}
        if self.ok:
            payload["data"] = self.data
            if self.summary:
                payload["summary"] = self.summary
        else:
            payload["error_code"] = self.error_code
            payload["message"] = self.error_message
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "data": self.data,
            "summary": self.summary,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "latency_ms": round(self.latency_ms, 1),
        }


class Tool(ABC):
    """工具基类。子类只需要声明四样东西：名字、说明、参数模型、handle()。"""

    #: 工具名，必须与蓝图 §8.2 / §4.3 逐字一致
    name: ClassVar[str]
    #: 给 LLM 看的说明：要写清"什么时候用"，而不只是"这是什么"
    description: ClassVar[str]
    #: Pydantic 参数模型（强校验）
    args_model: ClassVar[type[BaseModel]]
    #: 是否为写操作（会产生真实业务副作用）→ 触发二次确认与越权检查
    is_write: ClassVar[bool] = False
    #: 是否要求已认证读者身份
    requires_reader: ClassVar[bool] = False
    #: 是否属于"事实性数据"工具：有它才算"带工具证据的回答"（蓝图 §7 硬约束 100%）
    provides_evidence: ClassVar[bool] = True

    @abstractmethod
    async def handle(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行工具。实现里只管业务，超时/重试/熔断由 java_client 负责。"""

    # ---------------- schema 导出 ----------------
    def json_schema(self) -> dict[str, Any]:
        """给 /tools 与 LLM function calling 用的完整 JSON Schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
            "is_write": self.is_write,
            "requires_reader": self.requires_reader,
            "provides_evidence": self.provides_evidence,
        }

    def llm_spec(self) -> dict[str, Any]:
        """LLM 只需 name/description/parameters 三项。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }


__all__ = ["Tool", "ToolContext", "ToolResult", "ToolError"]
