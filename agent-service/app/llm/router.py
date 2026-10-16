"""LLM 路由：按配置选 provider，并做"真实 LLM 不可用时自动回落 Mock"的降级。

为什么需要 router 而不是直接 `if provider == ...`：
1. 降级策略是**工程护栏**的一部分（蓝图 §6.4 的"降级话术"），
   放在一处实现，所有调用点自动获得同样行为；
2. 埋点统一（耗时、token），否则 token 与延迟数据散落各处收不齐；
3. 供 /admin/metrics 暴露"当前用的哪个 provider、降级过几次"。
"""

from __future__ import annotations

from typing import Any

from app.config import Settings, get_settings
from app.llm.base import BaseLLM, ChatMessage, LLMResult
from app.llm.mock import MockLLM
from app.llm.openai_compat import OpenAICompatLLM
from app.metrics import METRICS


class LLMRouter:
    """带降级的 LLM 入口。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.primary: BaseLLM = self._build(self.settings.llm_provider)
        self.fallback: BaseLLM = MockLLM(self.settings)
        self.degrade_count = 0
        self.last_error: str | None = None

    def _build(self, provider: str) -> BaseLLM:
        if provider == "openai_compat":
            return OpenAICompatLLM(self.settings)
        return MockLLM(self.settings)

    @property
    def provider(self) -> str:
        return self.primary.provider

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        allow_fallback: bool = True,
    ) -> LLMResult:
        try:
            result = await self.primary.complete(messages, tools=tools)
        except Exception as exc:  # noqa: BLE001 - 降级必须兜住所有异常，否则助手整体不可用
            if not allow_fallback or self.primary.provider == "mock":
                raise
            self.degrade_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            METRICS.incr("llm_degraded")
            # 明确标记降级来源，评测报告里能区分"真实模型跑出来的分"与"降级跑出来的分"
            result = await self.fallback.complete(messages, tools=tools)
            result.provider = f"{result.provider}(degraded_from_{self.primary.provider})"
        result.latency_ms = max(result.latency_ms, 0.0)
        METRICS.observe_llm(result.latency_ms)
        METRICS.incr("llm_prompt_tokens", result.usage.get("prompt", 0))
        METRICS.incr("llm_completion_tokens", result.usage.get("completion", 0))
        return result

    async def aclose(self) -> None:
        await self.primary.aclose()
        if self.fallback is not self.primary:
            await self.fallback.aclose()

    def snapshot(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.settings.llm_model if self.provider == "openai_compat" else "mock",
            "degrade_count": self.degrade_count,
            "last_error": self.last_error,
            "fallback_provider": self.fallback.provider,
        }


def build_llm(settings: Settings | None = None) -> BaseLLM:
    """按配置直接构造（评测脚本用，不需要 router 的降级统计）。"""
    cfg = settings or get_settings()
    if cfg.llm_provider == "openai_compat":
        return OpenAICompatLLM(cfg)
    return MockLLM(cfg)


__all__ = ["LLMRouter", "build_llm", "BaseLLM", "ChatMessage", "LLMResult"]
