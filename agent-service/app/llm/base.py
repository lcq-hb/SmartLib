"""LLM 抽象层：统一契约，屏蔽 Mock 与真实 OpenAI 兼容服务的差异。

为什么要有这一层：
- 蓝图 §3.2 要求 `LLM_PROVIDER=mock` 时**整条链路离线可跑**（测试、评测、演示）；
- 上层（intent / planner / runtime）只依赖这里的抽象，切换 provider 不改业务代码；
- 工具调用的表示方式（tool_calls / tool 角色消息）统一成型，
  避免 MockLLM 与真实 LLM 的返回结构不一致导致 runtime 里到处 if-else。
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Literal

Role = Literal["system", "user", "assistant", "tool"]


def json_schema_hint(model_cls: type) -> dict[str, Any]:
    """把 Pydantic 模型的字段信息压成 LLM 能读懂的简版 schema。

    不用完整 JSON Schema 的原因：完整的太啰嗦，进 prompt 会显著抬高 token 成本，
    而我们只需要"参数名 + 类型 + 说明 + 是否必填"这几项。
    """
    hint: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    for name, fld in model_cls.model_fields.items():
        ann = fld.annotation
        type_name = getattr(ann, "__name__", str(ann))
        hint["properties"][name] = {
            "type": type_name,
            "description": fld.description or "",
        }
        if fld.is_required():
            hint["required"].append(name)
    return hint


@dataclass
class ToolCall:
    """一次工具调用意图。arguments 一定是 dict（已解析），不是 JSON 字符串。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class ChatMessage:
    role: Role
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return d


@dataclass
class LLMResult:
    """一次 LLM 调用的结果。finish_reason 决定 runtime 下一步做什么。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: "stop" | "tool_calls" | "length" | "error"
    finish_reason: str = "stop"
    #: {"prompt": n, "completion": n, "total": n}，Mock 用字符数近似，真实 provider 用返回值
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0
    provider: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "latency_ms": round(self.latency_ms, 1),
            "provider": self.provider,
        }


class BaseLLM(ABC):
    """所有 LLM 实现的最小契约。"""

    provider: str = "base"

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        """给定消息历史与可用工具，返回模型输出（可能是工具调用，也可能是文本）。"""

    @abstractmethod
    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """流式返回文本增量。有 tool_calls 时只产出空序列，由调用方再走 complete。"""
        raise NotImplementedError
        yield ""  # pragma: no cover - 让类型检查器知道这是异步生成器

    async def aclose(self) -> None:
        """释放底层资源（httpx client 等），默认无操作。"""
        return None


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文约 1 字 1 token，英文约 4 字符 1 token。

    只用于埋点趋势观察，不用于计费；精确值以 provider 返回的 usage 为准。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def timer() -> float:
    return time.perf_counter()


def elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def dump_json(obj: Any) -> str:
    """统一 JSON 序列化：中文不转义，便于人工阅读 trace 与日志。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def iter_text_chunks(text: str, size: int = 12) -> Iterable[str]:
    """把整段文本切成小块，模拟真实 LLM 的流式输出。"""
    for i in range(0, len(text), size):
        yield text[i : i + size]
