"""对话接口：SSE 流式 + 同步 JSON。

为什么两个都要：
- **流式（/chat）**是产品形态：读者不想等 3 秒才看到第一个字，
  蓝图 §7 的"首 Token 延迟 < 1.5s"只有在流式下才有意义；
- **同步（/chat/sync）**是工程形态：评测脚本、pytest、联调都需要一个
  "一次调用拿到完整结构化结果"的入口，用流式去凑结构化数据是在自找麻烦。

SSE 实现选择手写事件格式（而不是 sse-starlette）：
我们要精确控制 `event:` 名称与 `data:` 的 JSON 结构（前端按 type 分支），
就这几行代码，多一个依赖不划算。注意必须带
`X-Accel-Buffering: no`，否则 nginx 会把流缓冲成一整坨发出去，
"流式"就名存实亡——这是上线最容易踩的坑。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.agent.runtime import AgentRuntime
from app.api.schemas import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


def _runtime(request: Request) -> AgentRuntime:
    return request.app.state.runtime


@router.post("/chat/sync", response_model=ChatResponse)
async def chat_sync(payload: ChatRequest, request: Request) -> ChatResponse:
    """同步对话：一次返回完整结果（评测与测试的主入口）。

    trace_id 的优先级：请求体显式传入 > 中间件生成的（来自 X-Trace-Id 请求头或新生成）。
    这样"一次 HTTP 请求 = 一个 trace_id"，响应头与响应体里的值必然一致，
    排查时不会出现"头里一个 id、body 里另一个 id"的情况。
    """
    trace_id = payload.trace_id or getattr(request.state, "trace_id", None)
    result = await _runtime(request).run(
        payload.message,
        session_id=payload.session_id,
        reader_id=payload.reader_id,
        mode=payload.mode,
        trace_id=trace_id,
    )
    request.state.trace_id = result.trace_id
    request.app.state.record_trace(result.to_dict())
    return ChatResponse(**result.to_dict())


@router.post("/chat")
async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    """SSE 流式对话。

    事件类型：start / intent / tool_call / tool_result / token / done / error。
    每个 data 都是 JSON，最后一个 done 事件带完整结构化结果（含 trace_id）。
    """
    runtime = _runtime(request)

    async def event_source() -> AsyncIterator[str]:
        trace_holder: dict[str, Any] = {}
        try:
            async for event in runtime.run_stream(
                payload.message,
                session_id=payload.session_id,
                reader_id=payload.reader_id,
                mode=payload.mode,
                trace_id=payload.trace_id or getattr(request.state, "trace_id", None),
            ):
                if event.get("type") == "done":
                    result = event.get("result") or {}
                    trace_holder["trace_id"] = result.get("trace_id")
                    request.app.state.record_trace(result)
                elif event.get("type") == "start":
                    trace_holder["trace_id"] = event.get("trace_id")
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001 - 流里抛异常必须变成 error 事件，否则前端只能看到连接断开
            yield _sse({"type": "error", "message": f"服务内部错误：{type(exc).__name__}"})
        if trace_holder.get("trace_id"):
            request.state.trace_id = trace_holder["trace_id"]

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关键：禁止 nginx 缓冲 SSE，否则流式退化为一次性输出
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: dict[str, Any]) -> str:
    """把事件编成 SSE 帧：`event: <type>\\ndata: <json>\\n\\n`。"""
    event_type = str(event.get("type") or "message")
    return f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


__all__ = ["router"]
