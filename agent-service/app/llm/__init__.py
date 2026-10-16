"""LLM 抽象层：base（契约）/ mock（离线确定性）/ openai_compat（真实）/ router（降级路由）。"""

from app.llm.base import BaseLLM, ChatMessage, LLMResult, ToolCall
from app.llm.router import LLMRouter, build_llm

__all__ = ["BaseLLM", "ChatMessage", "LLMResult", "ToolCall", "LLMRouter", "build_llm"]
