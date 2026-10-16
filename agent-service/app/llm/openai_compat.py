"""OpenAI 兼容协议的 LLM 实现。

为什么只做"OpenAI 兼容"这一种真实实现：
蓝图 §3.2 明确 DeepSeek / 通义 / 本地 vLLM 都提供 OpenAI 兼容端点，
所以只要实现一次 `/chat/completions`，换 base_url + model 就能对接三家以上服务，
不需要为每家写适配器（这是选型收益，不是偷懒）。

关键处理：
- **工具调用兼容**：不同服务返回的 `tool_calls[].function.arguments` 都是 JSON 字符串，
  但有的会带 markdown 代码块或尾随逗号，这里做一次宽松解析，解析失败就退化为
  "无参数调用"，避免因为一个逗号把整轮对话打死。
- **错误不吞**：网络/限流/格式错误统一抛 LLMError，由 runtime 决定降级到规则路径。
  （hybrid 模式的价值就在这：LLM 挂了不等于助手挂了。）
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

import httpx

from app.config import Settings, get_settings
from app.errors import LLMError
from app.llm.base import (
    BaseLLM,
    ChatMessage,
    LLMResult,
    ToolCall,
    elapsed_ms,
    timer,
)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_loose_json(raw: str) -> dict[str, Any]:
    """宽松解析工具参数 JSON：容错 markdown 代码块与常见格式瑕疵。"""
    if not raw:
        return {}
    candidate = raw.strip()
    m = _JSON_BLOCK_RE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    for attempt in (candidate, candidate.rstrip(","), candidate.replace("'", '"')):
        try:
            parsed = json.loads(attempt)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    # 完全解析不了：返回空参数，让 Pydantic 校验去报一个清晰的参数错误，
    # 而不是在这里抛出难懂的 JSONDecodeError
    return {}


class OpenAICompatLLM(BaseLLM):
    """调用任意 OpenAI 兼容的 /chat/completions。"""

    provider = "openai_compat"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            if not self.settings.llm_api_key:
                # 提前失败，错误信息比 401 更可读
                raise LLMError(
                    "LLM_PROVIDER=openai_compat 但未配置 LLM_API_KEY",
                    user_message="智能助手暂时不可用（缺少模型配置），我先按规则为您服务。",
                )
            self._client = httpx.AsyncClient(
                base_url=self.settings.llm_base_url.rstrip("/"),
                timeout=httpx.Timeout(self.settings.llm_timeout_seconds),
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ---------------- 请求体 ----------------
    def _payload(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        *,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": self._to_wire(messages),
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = [self._tool_to_wire(t) for t in tools]
            payload["tool_choice"] = "auto"
        return payload

    @staticmethod
    def _to_wire(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for msg in messages:
            item: dict[str, Any] = {"role": msg.role, "content": msg.content or ""}
            if msg.role == "assistant" and msg.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.id or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for i, tc in enumerate(msg.tool_calls)
                ]
                # OpenAI 规范：带 tool_calls 的 assistant 消息 content 必须是 null 或空
                item["content"] = msg.content or ""
            if msg.role == "tool":
                item["tool_call_id"] = msg.tool_call_id or ""
                if msg.name:
                    item["name"] = msg.name
            wire.append(item)
        return wire

    @staticmethod
    def _tool_to_wire(spec: dict[str, Any]) -> dict[str, Any]:
        if "function" in spec:
            return spec
        return {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "parameters": spec.get("parameters", {"type": "object", "properties": {}}),
            },
        }

    # ---------------- 主流程 ----------------
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        start = timer()
        client = await self._get_client()
        payload = self._payload(
            messages, tools, temperature=temperature, max_tokens=max_tokens, stream=False
        )
        try:
            resp = await client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 网络错误: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
            choice = body["choices"][0]
            message = choice.get("message") or {}
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"LLM 响应格式非法: {exc}") from exc

        tool_calls: list[ToolCall] = []
        for idx, raw in enumerate(message.get("tool_calls") or []):
            fn = raw.get("function") or {}
            tool_calls.append(
                ToolCall(
                    name=fn.get("name", ""),
                    arguments=parse_loose_json(fn.get("arguments", "")),
                    id=raw.get("id") or f"call_{idx}",
                )
            )

        usage_raw = body.get("usage") or {}
        usage = {
            "prompt": int(usage_raw.get("prompt_tokens") or 0),
            "completion": int(usage_raw.get("completion_tokens") or 0),
            "total": int(usage_raw.get("total_tokens") or 0),
        }
        finish = choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
        return LLMResult(
            content=message.get("content") or "",
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=usage,
            latency_ms=elapsed_ms(start),
            provider=self.provider,
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """SSE 流式输出。

        注意：这里**不**在流里处理 tool_calls 的分片拼接 —— 那样状态机很复杂且容易出错。
        工程上的取舍是：runtime 先用非流式 complete 拿到决策（要不要调工具），
        工具都执行完、要出最终自然语言回答时，才切到流式。
        这样既有"首 Token 快"的体验，又不会出现"流到一半发现要调工具"的尴尬。
        """
        client = await self._get_client()
        payload = self._payload(
            messages, tools, temperature=temperature, max_tokens=None, stream=True
        )
        try:
            async with client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "ignore")
                    raise LLMError(f"LLM 流式返回 {resp.status_code}: {text[:200]}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta") or {}
                    except (ValueError, KeyError, IndexError):
                        continue
                    piece = delta.get("content")
                    if piece:
                        yield piece
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 流式网络错误: {exc}") from exc


__all__ = ["OpenAICompatLLM", "parse_loose_json"]
