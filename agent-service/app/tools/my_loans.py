"""my_loans：查询读者当前借阅（含逾期天数与可否续借）。

对应蓝图 §8.2：GET /internal/agent/tools/readers/{id}/loans
    → [{borrowId,title,dueDate,overdueDays,renewable}]

这个工具承担两个职责：
1. 直接回答"我借了什么书、什么时候还、有没有逾期"；
2. 作为 renew 的**前序步骤**——把书名映射成 borrowId（renew 只认借阅记录 ID）。
   所以它的返回里必须带 borrowId，且要保留书名，方便 runtime 做关联。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.client.java_client import JavaClient
from app.errors import CircuitOpenError, UpstreamTimeoutError
from app.tool_schemas import MyLoansArgs
from app.tools.base import Tool, ToolContext, ToolError, ToolResult


class MyLoansTool(Tool):
    name = "my_loans"
    description = (
        "查询读者本人的在借图书列表，包含书名、应还日期、逾期天数、是否可续借、借阅记录 ID。"
        "当读者问'我借了哪些书''什么时候到期''有没有逾期''我预约了什么'时使用。"
        "返回的 borrowId 是办理续借所需的参数。"
    )
    args_model = MyLoansArgs
    is_write = False
    requires_reader = True

    async def handle(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = MyLoansArgs.model_validate(args)
        reader_id = ctx.require_reader()

        # 纵深防御：护栏已经拦过一次，工具自己也必须再挡一次。
        # 越权查询他人借阅记录属于隐私问题，不能只依赖一层检查。
        if params.readerId != reader_id:
            raise ToolError(
                f"越权查询他人借阅记录: session_reader={reader_id} args_reader={params.readerId}",
                user_message="出于隐私保护，我只能查询您本人的借阅记录。",
                code="FORBIDDEN_OPERATION",
            )

        java: JavaClient | None = ctx.java
        if java is None:
            raise ToolError("未注入 JavaClient", user_message="系统未正确初始化，请联系管理员。")

        try:
            raw = await java.my_loans(reader_id=reader_id, trace_id=ctx.trace_id)
        except (CircuitOpenError, UpstreamTimeoutError) as exc:
            raise ToolError(
                f"借阅记录查询不可用: {exc}",
                user_message="借阅记录查询服务繁忙，我暂时看不到您的在借情况，请稍后再试。",
                code=exc.code,
            ) from exc

        loans = [self._normalize(item) for item in raw if isinstance(item, dict)]
        if params.only_overdue:
            loans = [item for item in loans if item["overdueDays"] > 0]

        overdue = [item for item in loans if item["overdueDays"] > 0]
        if not loans:
            summary = "没有逾期的在借图书。" if params.only_overdue else "当前没有在借图书。"
        else:
            summary = f"共 {len(loans)} 条在借记录"
            if overdue:
                summary += f"，其中 {len(overdue)} 条已逾期（最长 {max(i['overdueDays'] for i in overdue)} 天）"

        return ToolResult(
            ok=True,
            tool=self.name,
            data={"readerId": reader_id, "items": loans, "count": len(loans), "overdueCount": len(overdue)},
            summary=summary,
        )

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        overdue_days = item.get("overdueDays", item.get("overdue_days", 0)) or 0
        return {
            "borrowId": item.get("borrowId") or item.get("borrow_id") or item.get("id"),
            "bookId": item.get("bookId") or item.get("book_id"),
            "title": item.get("title") or "未知书名",
            "borrowAt": item.get("borrowAt") or item.get("borrow_at"),
            "dueDate": item.get("dueDate") or item.get("due_at") or item.get("dueAt"),
            "renewCount": int(item.get("renewCount", item.get("renew_count", 0)) or 0),
            "renewable": bool(item.get("renewable", False)),
            "overdueDays": int(overdue_days),
            "fine": item.get("fine"),
            "status": item.get("status") or "",
        }


TOOL = MyLoansTool()
