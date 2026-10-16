"""get_reader_profile：查询读者档案（类型/信用分/偏好分类/在借数量）。

对应蓝图 §8.2：GET /internal/agent/tools/readers/{id}/profile
    → {readerType,creditScore,preferredCategories[],borrowedCount}

为什么这个工具会被优先使用：
"我能借几本""我的信用分多少"这类问题不需要 LLM 推理，
拿到档案后按蓝图 §2.1 的规则表直接算就能回答，又准又快。
它同时也是"越权请求"的检测点——读者 A 问读者 B 的档案，护栏直接拒绝。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.client.java_client import JavaClient
from app.errors import CircuitOpenError, UpstreamTimeoutError
from app.tool_schemas import GetReaderProfileArgs
from app.tools.base import Tool, ToolContext, ToolError, ToolResult

#: 蓝图 §2.1 的规则表：读者类型 → (可借册数, 借期天数, 续借次数)
#: 放在工具里是因为"能借几本"的高频问法不值得再去后端问一次规则引擎。
READER_TYPE_RULES: dict[str, tuple[int, int, int]] = {
    "UNDERGRAD": (10, 30, 2),
    "POSTGRAD": (15, 60, 3),
    "TEACHER": (20, 90, 3),
}

READER_TYPE_LABELS: dict[str, str] = {
    "UNDERGRAD": "本科生",
    "POSTGRAD": "研究生",
    "TEACHER": "教师",
}


class GetReaderProfileTool(Tool):
    name = "get_reader_profile"
    description = (
        "查询读者本人档案：读者类型、信用分、常借分类偏好、当前在借数量，"
        "并附带该类型的可借册数与借期。当读者问'我能借几本''我的信用分'"
        "'我的借阅权限'时使用。"
    )
    args_model = GetReaderProfileArgs
    is_write = False
    requires_reader = True

    async def handle(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = GetReaderProfileArgs.model_validate(args)
        session_reader = ctx.require_reader()

        if params.readerId != session_reader:
            raise ToolError(
                f"越权查询他人档案: session_reader={session_reader} args_reader={params.readerId}",
                user_message="出于隐私保护，我只能查询您本人的借阅档案。",
                code="FORBIDDEN_OPERATION",
            )

        java: JavaClient | None = ctx.java
        if java is None:
            raise ToolError("未注入 JavaClient", user_message="系统未正确初始化，请联系管理员。")

        try:
            data = await java.reader_profile(reader_id=params.readerId, trace_id=ctx.trace_id)
        except (CircuitOpenError, UpstreamTimeoutError) as exc:
            raise ToolError(
                f"读者档案查询不可用: {exc}",
                user_message="读者档案查询服务繁忙，请稍后再试。",
                code=exc.code,
            ) from exc

        profile = self._normalize(data, params.readerId)
        rules = READER_TYPE_RULES.get(profile["readerType"], (10, 30, 2))
        profile["quota"] = {
            "maxBooks": rules[0],
            "loanDays": rules[1],
            "maxRenew": rules[2],
        }
        profile["readerTypeLabel"] = READER_TYPE_LABELS.get(profile["readerType"], profile["readerType"])
        profile["canBorrow"] = profile["creditScore"] >= 60

        remaining = max(0, rules[0] - profile["borrowedCount"])
        summary = (
            f"{profile['readerTypeLabel']}，信用分 {profile['creditScore']}，"
            f"在借 {profile['borrowedCount']} 册，还可借 {remaining} 册"
            + ("" if profile["canBorrow"] else "（信用分低于 60，暂不可借）")
        )
        return ToolResult(ok=True, tool=self.name, data=profile, summary=summary)

    @staticmethod
    def _normalize(data: dict[str, Any], reader_id: int) -> dict[str, Any]:
        if not data:
            raise ToolError(
                f"读者档案为空 reader_id={reader_id}",
                user_message="没有查到您的读者档案，请确认读者编号是否正确。",
                code="READER_NOT_FOUND",
            )
        reader_type = str(data.get("readerType") or data.get("reader_type") or "UNDERGRAD")
        return {
            "readerId": data.get("readerId") or data.get("reader_id") or reader_id,
            "readerType": reader_type.upper(),
            "creditScore": int(data.get("creditScore", data.get("credit_score", 100)) or 0),
            "preferredCategories": list(
                data.get("preferredCategories") or data.get("preferred_categories") or []
            ),
            "borrowedCount": int(data.get("borrowedCount", data.get("borrowed_count", 0)) or 0),
            "status": data.get("status") or "NORMAL",
        }


TOOL = GetReaderProfileTool()
