"""推荐接口。

与 recommend 工具的区别：
- 工具版服务于**对话**（读者说"推荐几本"），结果要塞回 LLM 组织语言；
- 接口版服务于**界面**（首页推荐位），直接返回结构化列表给前端渲染，
  不经过 LLM，延迟更低、也更省 token。

两者共用同一个 RecommendTool，所以推荐策略与降级逻辑只有一份实现。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.schemas import RecommendItem, RecommendRequest, RecommendResponse
from app.tools.base import ToolContext, ToolError
from app.tools.recommend import TOOL as recommend_tool

router = APIRouter(tags=["recommend"])


@router.post("/recommend", response_model=RecommendResponse)
async def recommend(payload: RecommendRequest, request: Request) -> RecommendResponse:
    trace_id = payload.trace_id or getattr(request.state, "trace_id", "")
    runtime = request.app.state.runtime
    ctx = ToolContext(
        trace_id=trace_id,
        reader_id=payload.reader_id,
        java=request.app.state.java_client,
        settings=runtime.settings,
    )
    args = recommend_tool.args_model.model_validate(
        {
            "book_id": payload.book_id,
            "category": payload.category,
            "limit": payload.limit,
        }
    )
    try:
        result = await recommend_tool.handle(args, ctx)
    except ToolError as exc:
        # 推荐失败不是错误页面，而是一个"空列表 + 说明"，前端可以直接展示
        return RecommendResponse(
            trace_id=trace_id,
            items=[],
            strategy=f"unavailable:{exc.code}",
            degraded=True,
        )

    data = result.data if isinstance(result.data, dict) else {}
    items = [
        RecommendItem(
            book_id=item.get("bookId"),
            title=str(item.get("title") or ""),
            author=item.get("author") or None,
            category=item.get("category") or None,
            reason=str(item.get("reason") or ""),
            available=item.get("available"),
        )
        for item in data.get("items") or []
        if isinstance(item, dict) and item.get("title")
    ]
    return RecommendResponse(
        trace_id=trace_id,
        items=items,
        strategy=str(data.get("strategy") or ""),
        degraded=bool(data.get("degraded")),
    )


__all__ = ["router"]
