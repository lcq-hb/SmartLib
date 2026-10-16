"""get_stock：查询某书目的实时库存与馆藏位置。

对应蓝图 §8.2：GET /internal/agent/tools/books/{id}/stock
    → {bookId,title,available,total,queueLen,shelfLocation}

为什么单独做一个工具而不是复用 search_books：
库存是**高频写**数据（蓝图 §5 设计要点：库存与书目分表，库存不能放缓存当唯一事实源），
读者问"还有没有"时必须是实时值。检索结果里的 availableCopies 可能带缓存延迟，
所以"确认能否借到"这一类问题一律走 get_stock 拿权威值。

**降级语义（与后端约定，极易出错）**：availableCopies == -1 表示
"库存暂时无法确认"（后端降级态），不是"没有库存"。二者话术必须分开：
- -1 → "库存暂时查不到，请稍后再试"（不能替读者下"借不到"的结论）
- 0  → "目前没有可借复本，可以预约"
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.client.java_client import JavaClient
from app.errors import CircuitOpenError, UpstreamTimeoutError
from app.tool_schemas import GetStockArgs
from app.tools.base import Tool, ToolContext, ToolError, ToolResult


class GetStockTool(Tool):
    name = "get_stock"
    description = (
        "查询指定书目（book_id）的实时库存：可借复本数、总册数、预约队列长度、馆藏位置/索书号。"
        "当读者问'这本书还有吗''在几楼''能不能借到'时使用。"
    )
    args_model = GetStockArgs
    is_write = False
    requires_reader = False

    async def handle(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = GetStockArgs.model_validate(args)
        java: JavaClient | None = ctx.java
        if java is None:
            raise ToolError("未注入 JavaClient", user_message="系统未正确初始化，请联系管理员。")

        try:
            data = await java.get_stock(book_id=params.book_id, trace_id=ctx.trace_id)
        except (CircuitOpenError, UpstreamTimeoutError) as exc:
            raise ToolError(
                f"库存查询不可用: {exc}",
                user_message="库存查询服务繁忙，我暂时无法确认这本书的可借状态，请稍后再试。",
                code=exc.code,
            ) from exc

        stock = self._normalize(data, params.book_id)

        # available == -1 是后端约定的"降级态：库存暂时无法确认"，
        # 与 available == 0（确实没有可借复本）语义完全不同。
        # 说错这一句会直接把能借的书劝退，所以这里必须分支处理。
        if stock["indeterminate"]:
            summary = f"《{stock['title']}》库存暂时无法确认（后端降级），不能判断是否可借"
        elif stock["available"] > 0:
            summary = f"《{stock['title']}》可借 {stock['available']} 册，位置：{stock['shelfLocation'] or '待确认'}"
        else:
            summary = f"《{stock['title']}》当前无可借复本，预约队列 {stock['queueLen']} 人"
        return ToolResult(ok=True, tool=self.name, data=stock, summary=summary)

    @staticmethod
    def _normalize(data: dict[str, Any], book_id: int) -> dict[str, Any]:
        if not data:
            raise ToolError(
                f"库存接口未返回数据 book_id={book_id}",
                user_message="没有查到这本书的馆藏信息，请确认书名或编号是否正确。",
                code="BOOK_NOT_FOUND",
            )
        raw_available = data.get("available", data.get("availableCopies"))
        available = int(raw_available if raw_available is not None else 0)
        # 真实后端 stock 返回的是 totalCopies / availableCopies（联调实测），
        # 蓝图 §8.2 写的是 total / available；两种字段名都兼容。
        total = data.get("total", data.get("totalCopies"))
        return {
            "bookId": data.get("bookId") or data.get("book_id") or book_id,
            "title": data.get("title") or "该书",
            "available": available,
            "total": int(total or 0),
            "queueLen": int(data.get("queueLen", data.get("queue_len", 0)) or 0),
            "shelfLocation": data.get("shelfLocation") or data.get("shelf_location") or "",
            "callNumber": data.get("callNumber") or data.get("call_number") or "",
            #: -1 = 后端降级，库存不可确认；绝不可当成 0（没有库存）
            "indeterminate": available < 0,
            "degraded": bool(data.get("degraded")) or available < 0,
            "degradeReason": data.get("degradeReason") or data.get("degrade_reason"),
        }


TOOL = GetStockTool()
