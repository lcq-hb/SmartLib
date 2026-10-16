"""borrow：办理借书（写操作）。

对应蓝图 §8.2：POST /internal/agent/tools/borrow
    {readerId,bookId,idempotencyKey} → {ok,borrowId,dueDate,reason}

这是整个 Agent 里**风险最高**的工具，工程上的四道闸门：
1. **身份闸门**：readerId 必须等于当前会话 readerId，禁止替他人借书（护栏强制）；
2. **确认闸门**：args.confirm 为 True 才真正下单。用户只说"帮我借"只是提出请求，
   Agent 必须先复述书目与读者身份并等一句明确的"确认"——因为借阅会产生真实的
   库存扣减与应还日期，误操作要人工去还书台撤销；
3. **幂等闸门**：idempotencyKey 必须由 Agent 生成并稳定复用。网络重试
   （java_client 会自动重试）时后端靠 idempotent_record 表保证只生效一次；
4. **降级闸门**：任何超时/熔断都要告诉读者"不确定是否成功，请稍后在'我的借阅'核对"，
   绝不能说"借阅失败"——因为请求可能已在后端落库（超时≠失败）。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.client.java_client import JavaClient
from app.errors import CircuitOpenError, UpstreamTimeoutError, friendly_reason
from app.tool_schemas import BorrowArgs
from app.tools.base import Tool, ToolContext, ToolError, ToolResult

TOOL_DESCRIPTION = (
    "为读者办理借书。只有当读者明确要求借某本书、并且已给出读者身份时才调用。"
    "这是写操作：调用前必须先把书名与读者复述给用户并取得明确确认（确认字样），"
    "否则不要把 confirm 置为 true。"
)


class BorrowTool(Tool):
    name = "borrow"
    description = TOOL_DESCRIPTION
    args_model = BorrowArgs
    is_write = True
    requires_reader = True

    async def handle(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = BorrowArgs.model_validate(args)
        reader_id = ctx.require_reader()

        # 意图再明确不过的越权尝试：即使护栏被绕过，工具自己也要再挡一次（纵深防御）
        if params.readerId != reader_id:
            raise ToolError(
                f"越权借书被拒绝: session_reader={reader_id} args_reader={params.readerId}",
                user_message="只能为您本人办理借阅，我没有权限操作其他读者的账号。",
                code="FORBIDDEN_OPERATION",
            )

        if not params.confirm:
            raise ToolError(
                "写操作缺少二次确认",
                user_message=(
                    f"借阅会产生真实的借阅记录，需要您确认一次。"
                    f"请回复“确认借阅”（读者 {reader_id}，书目 {params.bookId}）我再办理。"
                ),
                code="CONFIRMATION_REQUIRED",
            )

        java: JavaClient | None = ctx.java
        if java is None:
            raise ToolError("未注入 JavaClient", user_message="系统未正确初始化，请联系管理员。")

        try:
            data = await java.borrow(
                reader_id=params.readerId,
                book_id=params.bookId,
                idempotency_key=params.idempotencyKey,
                trace_id=ctx.trace_id,
            )
        except (CircuitOpenError, UpstreamTimeoutError) as exc:
            # 关键：超时不能判定为失败
            raise ToolError(
                f"借阅请求结果未知: {exc}",
                user_message=(
                    "借阅请求已经发出，但系统响应超时，我无法确认是否办理成功。"
                    "请稍后在“我的借阅”里核对，避免重复借阅。"
                ),
                code="BORROW_RESULT_UNKNOWN",
            ) from exc

        if not data.get("ok", True):
            reason = data.get("reason") or ""
            raise ToolError(
                f"借阅被后端拒绝: {reason}",
                user_message=friendly_reason(reason, "借阅没有成功，请稍后再试。"),
                code=reason or "BORROW_REJECTED",
            )

        due_date = data.get("dueDate") or data.get("due_date")
        return ToolResult(
            ok=True,
            tool=self.name,
            data={
                "borrowId": data.get("borrowId") or data.get("borrow_id"),
                "dueDate": due_date,
                "reason": data.get("reason"),
            },
            summary=f"借阅成功，借阅记录号 {data.get('borrowId', '-')}，应还日期 {due_date}",
        )


TOOL = BorrowTool()
