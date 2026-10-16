"""search_books：检索书目。

对应蓝图 §8.2：POST /internal/agent/tools/search-books
    {query,category,page,size,sortBy} → {items[],total,tookMs,engine,degraded}

工程要点：
- `degraded` 字段透传：后端检索引擎降级（ES 挂掉回落到 DB 直查）时，
  助手要如实告诉读者"结果可能不是最优排序"，而不是假装一切正常——这是可信度问题。
- size 上限在 schema 层已限制为 50，防止 Agent 一次把全馆书目拉回来。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.client.java_client import JavaClient
from app.errors import CircuitOpenError, UpstreamTimeoutError
from app.tool_schemas import SearchBooksArgs
from app.tools.base import Tool, ToolContext, ToolError, ToolResult


class SearchBooksTool(Tool):
    name = "search_books"
    description = (
        "按关键词检索馆藏书目，返回书名、作者、可借复本数与馆藏位置。"
        "当读者想找书、问'有没有某本书'、按主题/分类找书时使用。"
        "参数 query 必填，可传书名、作者或 ISBN。"
    )
    args_model = SearchBooksArgs
    is_write = False
    requires_reader = False

    async def handle(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = SearchBooksArgs.model_validate(args)
        java: JavaClient | None = ctx.java
        if java is None:
            raise ToolError("未注入 JavaClient", user_message="系统未正确初始化，请联系管理员。")

        try:
            data = await java.search_books(
                query=params.query,
                category=params.category,
                page=params.page,
                size=params.size,
                sort_by=params.sortBy,
                trace_id=ctx.trace_id,
            )
        except (CircuitOpenError, UpstreamTimeoutError) as exc:
            # 检索是最高频请求，它的降级体验最重要：明确告知 + 给出替代做法
            raise ToolError(
                f"检索引擎不可用: {exc}",
                user_message=(
                    "书目检索服务暂时不可用，我没法确认馆藏情况。"
                    "您可以稍后再试，或到 1 楼服务台/自助查询机现场检索。"
                ),
                code=exc.code,
            ) from exc

        items = self._normalize_items(data)
        total = int(data.get("total") or len(items))
        took_ms = data.get("tookMs") or data.get("took_ms")
        engine = data.get("engine") or "unknown"
        degraded = bool(data.get("degraded"))
        # 后端降级时给出的原因（如"ES 不可用，回落 DB 直查"）。
        # 保留它是因为"结果可能不完整"必须如实告知，不能只说"已降级"。
        degrade_reason = data.get("degradeReason") or data.get("degrade_reason")

        if not items:
            summary = f"没有检索到与“{params.query}”相关的书目。"
        else:
            # 降级时用"检索到"，正常时用"共 N 条"——措辞上就不承诺结果完整
            if degraded:
                summary = (
                    f"降级检索模式下找到 {len(items)} 条相关书目"
                    f"（{degrade_reason or '检索引擎已降级'}），结果可能不完整"
                )
            else:
                summary = f"共 {total} 条结果，返回前 {len(items)} 条（引擎 {engine}）"

        return ToolResult(
            ok=True,
            tool=self.name,
            data={
                "items": items,
                "total": total,
                "tookMs": took_ms,
                "engine": engine,
                "degraded": degraded,
                "degradeReason": degrade_reason,
            },
            summary=summary,
        )

    @staticmethod
    def _normalize_items(data: dict[str, Any]) -> list[dict[str, Any]]:
        """把后端可能的不同字段命名统一成前端好渲染的形状。

        为什么不直接用后端的原始 JSON：Java 侧字段名可能随版本微调
        （availableCopies / available_copies），Agent 层做一次归一，
        下游渲染与评测断言只认一套字段，避免后端改字段名就把助手打挂。
        """
        raw = data.get("items") or data.get("list") or []
        items: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            items.append(
                {
                    # 真实后端 search 返回的主键字段名是 `id`（联调实测：
                    # {"id":5,"isbn":...,"title":"算法导论",...}），蓝图 §8.2 没写死
                    # 这个字段名，所以三种写法都归一化，避免下游解析 book_id 落空。
                    "bookId": item.get("bookId") or item.get("book_id") or item.get("id"),
                    "title": item.get("title") or item.get("name") or "未知书名",
                    "author": item.get("author") or "",
                    "publisher": item.get("publisher") or "",
                    "publishYear": item.get("publishYear") or item.get("publish_year"),
                    "category": item.get("category") or item.get("categoryName") or "",
                    "availableCopies": item.get("availableCopies", item.get("available_copies")),
                    "totalCopies": item.get("totalCopies", item.get("total_copies")),
                    "shelfLocation": item.get("shelfLocation") or item.get("shelf_location") or "",
                    "score": item.get("score"),
                }
            )
        return items


TOOL = SearchBooksTool()
