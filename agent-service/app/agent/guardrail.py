"""护栏：把"LLM 可能会做坏事"变成"系统不允许做坏事"。

这里的每一条规则都对应蓝图 §6.4 工程化护栏的一项要求。
规则不是"提示词里写一句请勿越权"——提示词是概率性的，
护栏必须是**确定性的代码判定**，否则不叫护栏。

规则清单（12 条，全部有单测）：
  G-01 tool_whitelist          工具白名单：未注册的工具名一律拒绝
  G-02 argument_schema         参数强校验：Pydantic 不通过就要向用户追问，而不是硬着头皮调
  G-03 no_borrow_for_others    禁止替他人借书：readerId 必须 == 当前会话读者
  G-04 write_needs_confirm     写操作二次确认
  G-05 factual_needs_evidence  事实性结论必须有工具证据，否则严禁下结论
  G-06 sensitive_words         敏感与违规内容：拒答并引导
  G-07 output_length           输出长度限制
  G-08 loop_detection          循环检测
  G-09 degrade_fallback        降级话术（后端不可用时说人话，不暴露堆栈）
  G-10 handoff_to_human        低置信度 / 连续失败 / 敏感 → 转人工
  G-11 no_personal_data_leak   个人数据不外泄：只答本人数据相关问题
  G-12 scope_limit             能力边界：超出图书馆业务范围的问题礼貌拒绝

设计取舍：护栏返回**判定结果（Verdict）**而不是直接抛异常或直接改文本。
因为同一个判定在不同场景下的处理不同：
- 参数不合法 → 应该变成一句追问（而不是报错）；
- 越权 → 应该变成固定拒绝话术；
- 敏感 → 拒答 + 转人工。
调用方（runtime）拿着 Verdict 自己决定怎么表达，护栏只负责"能不能"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.config import Settings, get_settings
from app.tools import registry
from app.tools.base import ToolResult

#: 判定动作
Action = Literal["allow", "ask", "refuse", "replace", "truncate", "handoff"]

# ---------------- G-06 敏感词 ----------------
# 说明：这里只拦"与图书馆业务无关且必须拒答"的内容（政治敏感、违法违规、色情暴力等）。
# 用最小必要词表，避免误伤"解剖学""尸检报告"这类正当学术检索需求。
SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(色情|裸聊|约炮|成人视频|黄图)"),
    re.compile(r"(赌博|博彩|六合彩|洗钱|刷单|代考|枪手|作弊神器)"),
    re.compile(r"(毒品|冰毒|大麻交易|枪支|弹药|爆炸物)"),
    re.compile(r"(反政府|颠覆国家|恐怖组织|分裂国家)"),
    re.compile(r"(代写论文|代写作业|论文买卖|买答案)"),
)

#: G-12 能力边界：明显超出图书馆业务的请求
OUT_OF_SCOPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(帮我写代码|写代码|写个程序|写一段代码|写一个程序|写一篇小说|写作文|写情书|写诗)"),
    re.compile(r"(天气预报|今天天气|明天天气|股票行情|彩票号码|汇率)"),
    re.compile(r"(订.{0,6}机票|定.{0,6}机票|订.{0,6}酒店|定.{0,6}酒店|点外卖|打车|叫车)"),
)

#: G-10 转人工触发词（读者主动要求人工）
HUMAN_REQUEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(转人工|人工客服|找馆员|联系馆员|找老师|投诉)"),
)

#: 需要跑工具才能下结论的意图（G-05）。policy_qa 也算：
#: 规则类回答必须给出处，否则就是在凭印象说话。
FACTUAL_INTENTS: frozenset[str] = frozenset(
    {
        "search_book",
        "get_stock",
        "borrow_book",
        "renew_book",
        "my_loans",
        "reader_profile",
        "policy_qa",
        "recommend",
    }
)

REFUSAL_SENSITIVE = (
    "抱歉，这个问题超出了我的服务范围，我不能回答。"
    "我是图书馆智能助手，可以帮您查书、借还、续借、查馆藏位置和解答借阅规则。"
)

REFUSAL_OUT_OF_SCOPE = (
    "抱歉，这个我不太擅长。我是图书馆智能助手，主要负责馆藏检索、借阅办理、"
    "续借预约、逾期查询和借阅规则解答。您在图书馆方面有什么需要吗？"
)

NO_EVIDENCE_FALLBACK = (
    "这个我需要查一下图书馆系统的实时数据才能给您准确答复，"
    "刚才没能查到（{reason}）。为了不给您错误信息，我先不猜——"
    "请稍后再问我一次，或到 1 楼服务台确认。"
)

HANDOFF_TEMPLATE = (
    "这个问题我不太确定，为了不耽误您，我帮您转接人工馆员。"
    "您也可以补充更多信息（比如书名、读者编号），我再试一次。"
)


@dataclass
class Verdict:
    """护栏判定结果。"""

    action: Action
    rule: str
    message: str = ""
    #: 供 /admin/traces 与评测观察的补充信息
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "rule": self.rule, "message": self.message, "detail": self.detail}


@dataclass
class GuardrailDecision:
    """一次完整护栏检查的汇总（多条规则可能同时触发，取最严格的那条）。"""

    verdict: Verdict
    notes: list[str] = field(default_factory=list)

    @property
    def action(self) -> Action:
        return self.verdict.action

    @property
    def allowed(self) -> bool:
        return self.verdict.action == "allow"

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict.to_dict(), "notes": self.notes}


#: 动作优先级：数字越大越严格，冲突时取最严格的
_ACTION_PRIORITY: dict[str, int] = {
    "allow": 0,
    "truncate": 1,
    "replace": 2,
    "ask": 3,
    "handoff": 4,
    "refuse": 5,
}


def _pick(strictest: Verdict, candidate: Verdict, notes: list[str]) -> Verdict:
    if candidate.action == "allow":
        return strictest
    notes.append(f"{candidate.rule}:{candidate.action}")
    if _ACTION_PRIORITY[candidate.action] > _ACTION_PRIORITY[strictest.action]:
        return candidate
    return strictest


class Guardrail:
    """确定性护栏。无状态，可安全复用。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ---------------- 输入侧 ----------------
    def check_input(self, message: str) -> GuardrailDecision:
        """用户输入检查：敏感内容、超范围请求、主动要求人工。"""
        notes: list[str] = []
        verdict = Verdict("allow", "")

        if not message or not message.strip():
            return GuardrailDecision(
                Verdict("ask", "G-02_argument_schema", "您想咨询什么？可以直接说书名或者要办的事情。")
            )

        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(message):
                return GuardrailDecision(
                    Verdict("refuse", "G-06_sensitive_words", REFUSAL_SENSITIVE, pattern.pattern),
                    notes=["G-06_sensitive_words:refuse"],
                )

        for pattern in OUT_OF_SCOPE_PATTERNS:
            if pattern.search(message):
                verdict = _pick(
                    verdict,
                    Verdict("refuse", "G-12_scope_limit", REFUSAL_OUT_OF_SCOPE, pattern.pattern),
                    notes,
                )

        for pattern in HUMAN_REQUEST_PATTERNS:
            if pattern.search(message):
                verdict = _pick(
                    verdict,
                    Verdict(
                        "handoff",
                        "G-10_handoff_to_human",
                        "好的，我帮您转接人工馆员。您也可以先告诉我具体问题，我看看能不能直接解决。",
                    ),
                    notes,
                )

        # G-07 输入侧长度：超长输入本身可能是 prompt 注入尝试，直接拒绝
        if len(message) > 2000:
            verdict = _pick(
                verdict,
                Verdict("refuse", "G-07_output_length", "您输入的内容太长了，请精简到 2000 字以内。"),
                notes,
            )

        return GuardrailDecision(verdict, notes)

    # ---------------- 工具调用侧 ----------------
    def check_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        session_reader_id: int | None,
        confirmed: bool,
        evidence_checked: bool = True,
    ) -> GuardrailDecision:
        """工具调用前的检查。

        - `confirmed`：本次会话是否已有用户的明确确认；
        - `evidence_checked`：写操作是否**已经拿到过**"这本书存在且有馆藏信息"的
          工具证据。默认 True 是为了不破坏只读工具的调用方；
          写操作必须由 runtime 显式传入真实状态（见 G-13）。
        """
        notes: list[str] = []
        verdict = Verdict("allow", "")

        # G-01 工具白名单
        tool = registry.get(tool_name)
        if tool is None:
            return GuardrailDecision(
                Verdict(
                    "refuse",
                    "G-01_tool_whitelist",
                    "抱歉，我暂时没有这项能力，可以帮您查书、借还、续借和解答借阅规则。",
                    detail=f"未注册工具 {tool_name}；白名单={sorted(registry.names())}",
                ),
                notes=["G-01_tool_whitelist:refuse"],
            )

        # G-02 参数强校验（在 executor 里也会做一次，这里提前拦是为了给出更自然的话术）
        try:
            tool.args_model.model_validate(args or {})
        except Exception as exc:  # noqa: BLE001 - 只看校验通过与否
            verdict = _pick(
                verdict,
                Verdict(
                    "ask",
                    "G-02_argument_schema",
                    f"我还需要一些信息才能办理：{_humanize_validation(exc)}。",
                    detail=str(exc),
                ),
                notes,
            )

        # G-03 禁止替他人借书（写操作 + 个人数据类工具都必须绑定当前会话读者）
        # 判定顺序：**越权 > 缺前置证据 > 缺确认**。
        # 顺序是刻意排的：越权必须拒绝；而"书查不到/借不到"时也绝不能
        # 抛出"请回复确认"——读者确认了也必然失败（实测：404 之后仍追问确认借阅）。
        if tool.requires_reader:
            claimed = args.get("readerId") if isinstance(args, dict) else None
            if session_reader_id is None:
                verdict = _pick(
                    verdict,
                    Verdict(
                        "ask",
                        "G-11_no_personal_data_leak",
                        "我还不知道您是哪位读者，请先告诉我您的读者编号（例如 readerId=1001）。",
                    ),
                    notes,
                )
            elif claimed is not None and int(claimed) != int(session_reader_id):
                # 越权 = 直接拒绝，**立即返回**：绝不能再叠一条"请确认"的 ask，
                # 否则 runtime 会把它当成正常的待确认写操作排队（实测踩过这个坑，
                # 结果是越权请求反过来变成"请确认借阅"，性质完全错了）。
                return GuardrailDecision(
                    Verdict(
                        "refuse",
                        "G-03_no_borrow_for_others",
                        "为了账号安全，我只能帮您本人办理借阅相关业务，无法操作其他读者的账号。",
                        detail=f"session={session_reader_id} claimed={claimed} tool={tool_name}",
                    ),
                    notes=["G-03_no_borrow_for_others:refuse"],
                )

        # G-13 写操作必须有前置核验：拿不到"这本书存在且有可借复本"的证据时，
        # 不允许把它当成"待确认"抛给读者（否则读者确认之后必然失败）。
        if tool.is_write and not evidence_checked:
            return GuardrailDecision(
                Verdict(
                    "refuse",
                    "G-13_write_needs_prerequisite",
                    "我还需要先确认这本书的馆藏情况才能办理借阅或续借。"
                    "麻烦您核对一下书名，或者先让我帮您查一下这本书有没有可借复本。",
                    detail=f"tool={tool_name} evidence_checked=False",
                ),
                notes=["G-13_write_needs_prerequisite:refuse"],
            )

        # G-04 写操作二次确认
        if tool.is_write:
            if not confirmed:
                verdict = _pick(
                    verdict,
                    Verdict(
                        "ask",
                        "G-04_write_needs_confirm",
                        "这属于会产生实际影响的操作（会生成借阅记录/改变应还日期），"
                        "请回复“确认”后我再执行。",
                        detail=f"tool={tool_name} confirm={args.get('confirm') if isinstance(args, dict) else None}",
                    ),
                    notes,
                )

        return GuardrailDecision(verdict, notes)

    # ---------------- 输出侧 ----------------
    def check_output(
        self,
        reply: str,
        *,
        intent: str,
        has_evidence: bool,
        tool_error_codes: list[str] | None = None,
        handoff: bool = False,
        awaiting_confirm: bool = False,
        guardrail_blocked: bool = False,
    ) -> GuardrailDecision:
        """最终回答的检查：事实性结论必须有证据（G-05）、长度限制（G-07）。

        两个跳过 G-05 的情形（都是实测踩出来的）：
        - awaiting_confirm=True：本轮是"等用户确认写操作"。"请回复确认"是提问，
          不是事实性结论，用无证据话术替换掉会让读者看不到该确认什么。
        - guardrail_blocked=True：回复本身就是护栏给出的拒绝/转人工话术（权威文案），
          再被 G-05 覆盖一次，越权拒绝就变成了一句无关的兜底话术。
        """
        notes: list[str] = []
        verdict = Verdict("allow", "")

        if handoff:
            verdict = _pick(
                verdict, Verdict("handoff", "G-10_handoff_to_human", HANDOFF_TEMPLATE), notes
            )

        # G-05 事实性回答必须有工具证据（蓝图 §7 硬约束：比例 100%）
        if (
            intent in FACTUAL_INTENTS
            and not has_evidence
            and not awaiting_confirm
            and not guardrail_blocked
        ):
            reason = "、".join(tool_error_codes or []) or "没有拿到工具返回"
            verdict = _pick(
                verdict,
                Verdict(
                    "replace",
                    "G-05_factual_needs_evidence",
                    NO_EVIDENCE_FALLBACK.format(reason=reason),
                    detail=f"intent={intent} errors={tool_error_codes}",
                ),
                notes,
            )

        # G-06 输出侧敏感词兜底（防 LLM 在回答里"科普"违规内容）
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(reply or ""):
                verdict = _pick(
                    verdict,
                    Verdict("refuse", "G-06_sensitive_words", REFUSAL_SENSITIVE, pattern.pattern),
                    notes,
                )

        # G-07 输出长度
        limit = self.settings.max_output_chars
        if reply and len(reply) > limit:
            # 截断后的长度必须真的 <= limit：所以保留区要按"后缀长度 + 1"反推，
            # 不能直接用 limit-20 再拼一个 20 多字的后缀（那样会超出上限）。
            suffix = "\n…（内容较长已精简，需要更多细节请告诉我）"
            keep = max(0, limit - len(suffix) - 1)
            verdict = _pick(
                verdict,
                Verdict(
                    "truncate",
                    "G-07_output_length",
                    reply[:keep].rstrip() + suffix,
                    detail=f"len={len(reply)} limit={limit}",
                ),
                notes,
            )

        return GuardrailDecision(verdict, notes)

    def check_tool_results(self, results: list[ToolResult], intent: str) -> GuardrailDecision:
        """工具执行后的检查：全失败 → 降级话术（G-09）；连续失败 → 转人工（G-10）。"""
        notes: list[str] = []
        verdict = Verdict("allow", "")
        if not results:
            return GuardrailDecision(verdict, notes)

        failures = [r for r in results if not r.ok]
        if failures and len(failures) == len(results):
            first = failures[0]
            verdict = _pick(
                verdict,
                Verdict(
                    "replace",
                    "G-09_degrade_fallback",
                    first.error_message
                    or "图书馆系统暂时不可用，我这次没能查到数据，请稍后再试。",
                    detail=f"all_failed codes={[f.error_code for f in failures]}",
                ),
                notes,
            )
        return GuardrailDecision(verdict, notes)

    # ---------------- 循环 / 转人工 ----------------
    def check_loop(self, fingerprints: dict[str, int], limit: int | None = None) -> GuardrailDecision:
        """G-08 循环检测。"""
        cap = limit or self.settings.loop_repeat_limit
        for fingerprint, count in fingerprints.items():
            if count > cap:
                return GuardrailDecision(
                    Verdict(
                        "replace",
                        "G-08_loop_detection",
                        "我在反复尝试同一个操作但没有成功，先停下来。"
                        "请换个说法，或到 1 楼服务台由馆员协助处理。",
                        detail=f"fingerprint={fingerprint} count={count}",
                    ),
                    notes=["G-08_loop_detection:replace"],
                )
        return GuardrailDecision(Verdict("allow", ""))

    def check_confidence(self, confidence: float, intent: str) -> GuardrailDecision:
        """G-10 低置信度转人工。"""
        if confidence < self.settings.low_confidence_threshold:
            return GuardrailDecision(
                Verdict(
                    "handoff",
                    "G-10_handoff_to_human",
                    HANDOFF_TEMPLATE,
                    detail=f"intent={intent} confidence={confidence:.2f}",
                ),
                notes=["G-10_handoff_to_human:handoff"],
            )
        return GuardrailDecision(Verdict("allow", ""))

    def needs_evidence(self, intent: str) -> bool:
        """该意图是否属于"必须带工具证据"的事实性问题。"""
        return intent in FACTUAL_INTENTS


def _humanize_validation(exc: Exception) -> str:
    """把 Pydantic 报错转成"还需要哪些信息"的自然语言提示。"""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    missing: list[str] = []
    for err in errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        if err.get("type") == "missing":
            missing.append(loc)
        elif loc:
            missing.append(f"{loc}（格式不对）")
    if not missing:
        return "参数格式不正确"
    return "、".join(dict.fromkeys(missing))


__all__ = [
    "Guardrail",
    "GuardrailDecision",
    "Verdict",
    "SENSITIVE_PATTERNS",
    "OUT_OF_SCOPE_PATTERNS",
    "FACTUAL_INTENTS",
    "REFUSAL_SENSITIVE",
    "REFUSAL_OUT_OF_SCOPE",
    "NO_EVIDENCE_FALLBACK",
    "HANDOFF_TEMPLATE",
]
