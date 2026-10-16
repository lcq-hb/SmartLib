"""renew：办理续借（写操作）。

对应蓝图 §8.2：POST /internal/agent/tools/renew
    {readerId,borrowId} → {ok,newDueDate,reason}

注意 renew 的入参是 **borrowId（借阅记录 ID）而不是 bookId**。
这是实践中最容易搞错的地方：读者说"续借《算法导论》"，Agent 必须先调 my_loans
把书名映射成借阅记录 ID，再调 renew。runtime 的 planner 负责这一步拆解
（蓝图 §6.4 G4 的多步任务示例就是这个场景）。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.client.java_client import JavaClient
from app.errors import CircuitOpenError, UpstreamTimeoutError, friendly_reason
from app.tool_schemas import RenewArgs
from app.tools.base import Tool, ToolContext, ToolError, ToolResult


class RenewTool(Tool):
    name = "renew"
    description = (
        "为读者办理图书续借。入参 borrowId 是**借阅记录 ID**（不是书目 ID）；"
        "若读者只说了书名，必须先调用 my_loans 查到对应借阅记录再续借。"
        "写操作，需要用户明确确认；续借要求该书无人预约、次数未用尽、未逾期。"
    )
    args_model = RenewArgs
    is_write = True
    requires_reader = True

    async def handle(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = RenewArgs.model_validate(args)
        reader_id = ctx.require_reader()

        if params.readerId != reader_id:
            raise ToolError(
                f"越权续借被拒绝: session_reader={reader_id} args_reader={params.readerId}",
                user_message="只能为您本人办理续借，我没有权限操作其他读者的账号。",
                code="FORBIDDEN_OPERATION",
            )

        if not params.confirm:
            raise ToolError(
                "写操作缺少二次确认",
                user_message=(
                    f"续借会改变应还日期，需要您确认一次。"
                    f"请回复“确认续借”（借阅记录 {params.borrowId}）我再办理。"
                ),
                code="CONFIRMATION_REQUIRED",
            )

        java: JavaClient | None = ctx.java
        if java is None:
            raise ToolError("未注入 JavaClient", user_message="系统未正确初始化，请联系管理员。")

        try:
            data = await java.renew(
                reader_id=params.readerId,
                borrow_id=params.borrowId,
                idempotency_key=params.idempotencyKey,
                trace_id=ctx.trace_id,
            )
        except (CircuitOpenError, UpstreamTimeoutError) as exc:
            raise ToolError(
                f"续借请求结果未知: {exc}",
                user_message=(
                    "续借请求已经发出，但系统响应超时，我无法确认是否成功。"
                    "请稍后在“我的借阅”里核对新的应还日期。"
                ),
                code="RENEW_RESULT_UNKNOWN",
            ) from exc

        if not data.get("ok", True):
            reason = data.get("reason") or ""
            raise ToolError(
                f"续借被后端拒绝: {reason}",
                user_message=friendly_reason(reason, "续借没有成功，请稍后再试。"),
                code=reason or "RENEW_REJECTED",
            )

        new_due = data.get("newDueDate") or data.get("new_due_date")
        return ToolResult(
            ok=True,
            tool=self.name,
            data={"newDueDate": new_due, "reason": data.get("reason")},
            summary=f"续借成功，新的应还日期是 {new_due}",
        )


TOOL = RenewTool()
