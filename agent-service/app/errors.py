"""统一异常与"给用户看的友好中文话术"。

为什么单独建一个模块：
- 工具层需要区分"参数错(用户自己改)"和"后端错(用户改不了)"，
  前者应当直接告诉用户怎么改，后者应当兜底成降级话术而不是抛 500。
- 把这些映射集中在这里，避免每个工具各写一份中文错误文案导致口径不一致。
"""

from __future__ import annotations


class SmartLibError(Exception):
    """Agent 服务内部异常基类。"""

    #: 给用户看的友好中文提示（会被直接写进回复里）
    user_message: str = "抱歉，服务暂时不可用，请稍后再试。"
    #: 机器可读错误码，用于埋点与评测断言
    code: str = "SMARTLIB_ERROR"

    def __init__(self, message: str = "", *, user_message: str | None = None) -> None:
        super().__init__(message or self.user_message)
        self.detail = message
        if user_message:
            self.user_message = user_message

    def to_payload(self) -> dict:
        return {"code": self.code, "detail": self.detail or str(self)}


class ToolArgumentError(SmartLibError):
    """工具参数校验失败：属于用户可纠正的错误，直接提示怎么改。"""

    code = "TOOL_ARGUMENT_INVALID"
    user_message = "您提供的信息不完整或格式不对，请补充后我再帮您办理。"


class ToolNotFoundError(SmartLibError):
    """请求了白名单之外的工具（护栏会先拦，这里是兜底）。"""

    code = "TOOL_NOT_FOUND"
    user_message = "抱歉，我暂时没有这项能力。"


class ForbiddenOperationError(SmartLibError):
    """越权操作：例如替他人借书。"""

    code = "FORBIDDEN_OPERATION"
    user_message = "为了账号安全，我只能帮您本人办理借阅相关业务，无法操作其他读者的账号。"

    def __init__(self, message: str = "", *, user_message: str | None = None) -> None:
        # 默认给一句更贴场景的话术，调用方可覆盖
        super().__init__(
            message,
            user_message=user_message
            or "为了账号安全，我只能帮您本人办理借阅相关业务，无法操作其他读者的账号。",
        )


class ConfirmationRequiredError(SmartLibError):
    """写操作缺少二次确认。"""

    code = "CONFIRMATION_REQUIRED"
    user_message = "这属于会产生实际影响的操作，需要您明确回复“确认”后我才会执行。"


class UpstreamError(SmartLibError):
    """Java 后端返回业务失败或非 2xx。"""

    code = "UPSTREAM_ERROR"
    user_message = "图书馆系统暂时繁忙，请稍后再试。"


class UpstreamTimeoutError(UpstreamError):
    """调用后端超时。"""

    code = "UPSTREAM_TIMEOUT"
    user_message = "查询超时了，可能是系统繁忙，请稍后再试一次。"


class CircuitOpenError(UpstreamError):
    """熔断器处于 OPEN 状态，直接快速失败，不再打后端。"""

    code = "CIRCUIT_OPEN"
    user_message = "图书馆系统正在恢复中，请稍等十几秒再试。"


class LLMError(SmartLibError):
    """LLM 调用失败（网络、限流、返回格式非法）。"""

    code = "LLM_ERROR"
    user_message = "我这边理解您的意思时出了点问题，请换个说法再问一次。"


#: 后端错误码 → 用户话术。后端 AgentToolController 的 reason 字段若是这些码，
#: 直接换成读者能懂的话；未收录的码统一走 UpstreamError 的兜底话术。
BUSINESS_REASON_MESSAGES: dict[str, str] = {
    "BOOK_NOT_FOUND": "没有找到这本书，麻烦您确认一下书名或 ISBN。",
    "NO_AVAILABLE_COPY": "这本书目前没有可借复本，我可以帮您预约排队。",
    "READER_NOT_FOUND": "没有查到您的读者账号，请确认读者编号是否正确。",
    "CREDIT_TOO_LOW": "您的信用分低于 60，暂时无法借书（仍可查询和预约）。",
    "HAS_OVERDUE": "您有待归还的逾期图书，归还后才能继续借阅。",
    "BORROW_LIMIT_EXCEEDED": "您当前的可借册数已用完，先还一部分再来借吧。",
    "DUPLICATE_BORROW": "同一本书您手上已经有一册了，不能重复借阅。",
    "FINE_UNPAID": "您有未缴清的逾期罚金（超过 10 元），缴清后即可继续借阅。",
    "RESERVATION_EXISTS": "这本书您已经预约过了，不需要重复预约。",
    "RENEW_NOT_ALLOWED_RESERVED": "这本书有人在预约排队，所以不能续借。",
    "RENEW_LIMIT_EXCEEDED": "您的续借次数已经用完了。",
    "RENEW_NOT_ALLOWED_OVERDUE": "这本书已经逾期，续借被拒绝，请先归还。",
    "BORROW_RECORD_NOT_FOUND": "没有找到对应的借阅记录，请确认要续借的是哪一本。",
    "IDEMPOTENT_REPLAY": "该请求已经处理过了，无需重复提交。",
}


def friendly_reason(reason: str | None, default: str = "") -> str:
    """把后端业务失败原因翻译成读者能看懂的中文。"""
    if not reason:
        return default or "操作没有成功，请稍后再试。"
    return BUSINESS_REASON_MESSAGES.get(reason, default or "操作没有成功，请稍后再试。")
