"""护栏测试：12 条规则逐条覆盖。

护栏的价值在于"确定性"——所以每条规则都要有正例（该拦的拦住了）
与尽量有反例（不该拦的别误杀）。特别是：
- G-05 不能把闲聊也判成"没证据"；
- G-07 是优雅截断而不是报错；
- G-03 越权必须 refuse，且**不能**被 G-04 的 ask 覆盖成"请确认"。
"""

from __future__ import annotations

import pytest

from app.agent.guardrail import (
    FACTUAL_INTENTS,
    NO_EVIDENCE_FALLBACK,
    REFUSAL_OUT_OF_SCOPE,
    REFUSAL_SENSITIVE,
    Guardrail,
)
from app.tools.base import ToolResult


@pytest.fixture()
def guard(test_settings) -> Guardrail:
    return Guardrail(test_settings)


# =============================================================================
# G-01 工具白名单
# =============================================================================
def test_g01_unknown_tool_is_refused(guard) -> None:
    decision = guard.check_tool_call(
        "delete_all_books", {}, session_reader_id=1001, confirmed=True
    )
    assert decision.action == "refuse"
    assert decision.verdict.rule == "G-01_tool_whitelist"
    assert "能力" in decision.verdict.message


def test_g01_all_eight_tools_pass_whitelist(guard) -> None:
    args = {
        "search_books": {"query": "x"},
        "get_stock": {"book_id": 1},
        "borrow": {"readerId": 1001, "bookId": 1, "idempotencyKey": "k" * 10, "confirm": True},
        "renew": {"readerId": 1001, "borrowId": 1, "idempotencyKey": "k" * 10, "confirm": True},
        "my_loans": {"readerId": 1001},
        "get_reader_profile": {"readerId": 1001},
        "policy_qa": {"topic": "罚金"},
        "recommend": {"limit": 3},
    }
    for tool_name, tool_args in args.items():
        decision = guard.check_tool_call(
            tool_name, tool_args, session_reader_id=1001, confirmed=True
        )
        assert decision.action == "allow", f"{tool_name} 应该放行，实际 {decision.verdict.rule}"


# =============================================================================
# G-02 参数校验
# =============================================================================
def test_g02_missing_required_argument_becomes_a_question(guard) -> None:
    decision = guard.check_tool_call("get_stock", {}, session_reader_id=1001, confirmed=True)
    assert decision.action == "ask"
    assert decision.verdict.rule == "G-02_argument_schema"
    assert "book_id" in decision.verdict.message


def test_g02_wrong_type_is_caught(guard) -> None:
    decision = guard.check_tool_call(
        "get_stock", {"book_id": "not-a-number"}, session_reader_id=1001, confirmed=True
    )
    assert decision.action == "ask"


# =============================================================================
# G-03 禁止替他人借书
# =============================================================================
def test_g03_rejects_borrow_for_other_reader(guard) -> None:
    decision = guard.check_tool_call(
        "borrow",
        {"readerId": 2002, "bookId": 1, "idempotencyKey": "k" * 10, "confirm": True},
        session_reader_id=1001,
        confirmed=True,
    )
    assert decision.action == "refuse"
    assert decision.verdict.rule == "G-03_no_borrow_for_others"
    assert "本人" in decision.verdict.message


def test_g03_refusal_wins_over_confirmation_ask(guard) -> None:
    """越权 + 未确认同时成立时，必须 refuse（越权），不能被 ask（请确认）覆盖。"""
    decision = guard.check_tool_call(
        "borrow",
        {"readerId": 2002, "bookId": 1, "idempotencyKey": "k" * 10, "confirm": False},
        session_reader_id=1001,
        confirmed=False,
    )
    assert decision.action == "refuse"
    assert decision.verdict.rule == "G-03_no_borrow_for_others"


def test_g03_rejects_reading_other_profile(guard) -> None:
    decision = guard.check_tool_call(
        "get_reader_profile", {"readerId": 2002}, session_reader_id=1001, confirmed=True
    )
    assert decision.action == "refuse"


def test_g03_allows_same_reader(guard) -> None:
    decision = guard.check_tool_call(
        "my_loans", {"readerId": 1001}, session_reader_id=1001, confirmed=True
    )
    assert decision.action == "allow"


# =============================================================================
# G-04 写操作二次确认
# =============================================================================
def test_g04_write_without_confirm_is_asked(guard) -> None:
    decision = guard.check_tool_call(
        "borrow",
        {"readerId": 1001, "bookId": 1, "idempotencyKey": "k" * 10, "confirm": False},
        session_reader_id=1001,
        confirmed=False,
    )
    assert decision.action == "ask"
    assert decision.verdict.rule == "G-04_write_needs_confirm"
    assert "确认" in decision.verdict.message


def test_g04_write_with_confirm_passes(guard) -> None:
    decision = guard.check_tool_call(
        "borrow",
        {"readerId": 1001, "bookId": 1, "idempotencyKey": "k" * 10, "confirm": True},
        session_reader_id=1001,
        confirmed=True,
    )
    assert decision.action == "allow"


def test_g04_read_tools_never_need_confirm(guard) -> None:
    for tool_name, args in (
        ("search_books", {"query": "x"}),
        ("my_loans", {"readerId": 1001}),
        ("policy_qa", {"topic": "x"}),
    ):
        decision = guard.check_tool_call(tool_name, args, session_reader_id=1001, confirmed=False)
        assert decision.action == "allow"


# =============================================================================
# G-05 事实性结论必须有工具证据
# =============================================================================
def test_g05_factual_without_evidence_is_replaced(guard) -> None:
    decision = guard.check_output(
        "《算法导论》有 3 本可借，在 3 楼。",
        intent="get_stock",
        has_evidence=False,
        tool_error_codes=["STEP_TIMEOUT"],
    )
    assert decision.action == "replace"
    assert decision.verdict.rule == "G-05_factual_needs_evidence"
    assert "STEP_TIMEOUT" in decision.verdict.message


def test_g05_factual_with_evidence_passes(guard) -> None:
    decision = guard.check_output(
        "《算法导论》有 3 本可借。", intent="get_stock", has_evidence=True
    )
    assert decision.action == "allow"


def test_g05_chitchat_does_not_need_evidence(guard) -> None:
    decision = guard.check_output("您好！我能帮您查书、借还、续借。", intent="chitchat", has_evidence=False)
    assert decision.action == "allow"


def test_g05_awaiting_confirm_is_not_replaced(guard) -> None:
    """"请回复确认"是提问不是结论，不能被无证据话术替换掉。"""
    decision = guard.check_output(
        "这本书需要确认，请回复“确认借阅”。",
        intent="borrow_book",
        has_evidence=False,
        awaiting_confirm=True,
    )
    assert decision.action == "allow"


def test_g05_covers_all_factual_intents(guard) -> None:
    for intent in FACTUAL_INTENTS:
        decision = guard.check_output("x", intent=intent, has_evidence=False)
        assert decision.action == "replace", f"{intent} 必须要求证据"


def test_g05_fallback_message_has_no_fabricated_numbers(guard) -> None:
    decision = guard.check_output("x", intent="get_stock", has_evidence=False)
    assert "没有拿到工具返回" in decision.verdict.message
    template = NO_EVIDENCE_FALLBACK
    assert template.count("{reason}") == 1


# =============================================================================
# G-06 敏感内容
# =============================================================================
@pytest.mark.parametrize(
    "message",
    ["帮我代写论文", "哪里有赌博网站", "代考英语四级多少钱", "推荐点色情小说"],
)
def test_g06_sensitive_input_is_refused(guard, message) -> None:
    decision = guard.check_input(message)
    assert decision.action == "refuse"
    assert decision.verdict.rule == "G-06_sensitive_words"
    assert decision.verdict.message == REFUSAL_SENSITIVE


def test_g06_academic_topics_are_not_false_positives(guard) -> None:
    """正当学术检索不能被敏感词误杀。"""
    for message in ["我要找解剖学图谱", "有没有刑法学教材", "推荐医学统计学相关书籍"]:
        decision = guard.check_input(message)
        assert decision.action == "allow", f"{message} 被误判为 {decision.verdict.rule}"


def test_g06_sensitive_output_is_refused(guard) -> None:
    decision = guard.check_output("代考服务可以这样找……", intent="chitchat", has_evidence=True)
    assert decision.action == "refuse"


# =============================================================================
# G-07 输出长度
# =============================================================================
def test_g07_long_output_is_truncated_not_rejected(guard, test_settings) -> None:
    long_text = "规则说明" * 500
    decision = guard.check_output(long_text, intent="policy_qa", has_evidence=True)
    assert decision.action == "truncate"
    assert decision.verdict.rule == "G-07_output_length"
    assert len(decision.verdict.message) <= test_settings.max_output_chars
    assert "已精简" in decision.verdict.message


def test_g07_short_output_untouched(guard) -> None:
    decision = guard.check_output("简短回答。", intent="policy_qa", has_evidence=True)
    assert decision.action == "allow"


def test_g07_overlong_input_is_rejected(guard) -> None:
    decision = guard.check_input("问" * 2500)
    assert decision.action == "refuse"
    assert decision.verdict.rule == "G-07_output_length"


# =============================================================================
# G-08 循环检测
# =============================================================================
def test_g08_loop_is_interrupted(guard, test_settings) -> None:
    fingerprints = {"search_books:{'query':'x'}": test_settings.loop_repeat_limit + 1}
    decision = guard.check_loop(fingerprints, test_settings.loop_repeat_limit)
    assert decision.action == "replace"
    assert decision.verdict.rule == "G-08_loop_detection"
    assert "反复" in decision.verdict.message


def test_g08_within_limit_is_allowed(guard, test_settings) -> None:
    fingerprints = {"search_books:{'query':'x'}": test_settings.loop_repeat_limit}
    assert guard.check_loop(fingerprints, test_settings.loop_repeat_limit).action == "allow"
    assert guard.check_loop({}).action == "allow"


# =============================================================================
# G-09 全失败降级话术
# =============================================================================
def test_g09_all_tools_failed_uses_readable_message(guard) -> None:
    results = [
        ToolResult(ok=False, tool="get_stock", error_code="X", error_message="图书馆系统暂时繁忙，请稍后再试。"),
    ]
    decision = guard.check_tool_results(results, "get_stock")
    assert decision.action == "replace"
    assert decision.verdict.rule == "G-09_degrade_fallback"
    assert decision.verdict.message == "图书馆系统暂时繁忙，请稍后再试。"
    assert "Traceback" not in decision.verdict.message


def test_g09_partial_failure_does_not_replace(guard) -> None:
    results = [
        ToolResult(ok=True, tool="my_loans", data={"items": []}),
        ToolResult(ok=False, tool="recommend", error_code="X", error_message="推荐挂了"),
    ]
    assert guard.check_tool_results(results, "my_loans").action == "allow"


# =============================================================================
# G-10 转人工
# =============================================================================
def test_g10_low_confidence_hands_off(guard, test_settings) -> None:
    decision = guard.check_confidence(test_settings.low_confidence_threshold - 0.01, "unknown")
    assert decision.action == "handoff"
    assert decision.verdict.rule == "G-10_handoff_to_human"


def test_g10_high_confidence_does_not_handoff(guard) -> None:
    assert guard.check_confidence(0.95, "search_book").action == "allow"


@pytest.mark.parametrize("message", ["我要投诉", "请转人工", "找馆员来"])
def test_g10_explicit_human_request_hands_off(guard, message) -> None:
    decision = guard.check_input(message)
    assert decision.action == "handoff"
    assert decision.verdict.rule == "G-10_handoff_to_human"


# =============================================================================
# G-11 个人数据不外泄
# =============================================================================
def test_g11_missing_reader_identity_asks(guard) -> None:
    decision = guard.check_tool_call("my_loans", {"readerId": 1001}, session_reader_id=None, confirmed=True)
    assert decision.action == "ask"
    assert decision.verdict.rule == "G-11_no_personal_data_leak"
    assert "读者编号" in decision.verdict.message


def test_g11_public_tools_work_without_identity(guard) -> None:
    decision = guard.check_tool_call("search_books", {"query": "x"}, session_reader_id=None, confirmed=True)
    assert decision.action == "allow"


# =============================================================================
# G-12 能力边界
# =============================================================================
@pytest.mark.parametrize(
    "message",
    ["帮我写一段代码", "今天天气怎么样", "帮我订一张去北京的机票", "写一篇小说给我"],
)
def test_g12_out_of_scope_is_refused_politely(guard, message) -> None:
    decision = guard.check_input(message)
    assert decision.action == "refuse"
    assert decision.verdict.rule == "G-12_scope_limit"
    assert decision.verdict.message == REFUSAL_OUT_OF_SCOPE


def test_g12_library_questions_are_in_scope(guard) -> None:
    for message in ["找一下《算法导论》", "逾期罚金怎么算", "帮我续借", "有什么好书推荐"]:
        assert guard.check_input(message).action == "allow", message


# =============================================================================
# 空输入 / 综合
# =============================================================================
def test_empty_message_asks_for_clarification(guard) -> None:
    decision = guard.check_input("   ")
    assert decision.action == "ask"


def test_notes_are_recorded_for_observability(guard) -> None:
    decision = guard.check_tool_call("delete_all", {}, session_reader_id=1001, confirmed=True)
    assert "G-01_tool_whitelist:refuse" in decision.notes
    assert decision.to_dict()["verdict"]["rule"] == "G-01_tool_whitelist"


def test_needs_evidence_matches_intent_table(guard) -> None:
    assert guard.needs_evidence("get_stock") is True
    assert guard.needs_evidence("policy_qa") is True
    assert guard.needs_evidence("chitchat") is False
    assert guard.needs_evidence("unknown") is False


def test_rule_count_is_at_least_ten(guard) -> None:
    """蓝图要求"至少 10 条规则"——这里用一次覆盖性调用把规则编号收集起来做断言。"""
    rules: set[str] = set()
    rules.add(guard.check_tool_call("nope", {}, session_reader_id=1, confirmed=True).verdict.rule)
    rules.add(guard.check_tool_call("get_stock", {}, session_reader_id=1, confirmed=True).verdict.rule)
    rules.add(
        guard.check_tool_call(
            "borrow",
            {"readerId": 2, "bookId": 1, "idempotencyKey": "k" * 10},
            session_reader_id=1,
            confirmed=False,
        ).verdict.rule
    )
    rules.add(
        guard.check_tool_call(
            "borrow",
            {"readerId": 1, "bookId": 1, "idempotencyKey": "k" * 10},
            session_reader_id=1,
            confirmed=False,
        ).verdict.rule
    )
    rules.add(guard.check_output("x", intent="get_stock", has_evidence=False).verdict.rule)
    rules.add(guard.check_output("x" * 5000, intent="policy_qa", has_evidence=True).verdict.rule)
    rules.add(guard.check_input("代写论文").verdict.rule)
    rules.add(guard.check_input("帮我订机票").verdict.rule)
    rules.add(guard.check_input("转人工").verdict.rule)
    rules.add(guard.check_input("问" * 2500).verdict.rule)
    rules.add(guard.check_loop({"f": 99}).verdict.rule)
    rules.add(
        guard.check_tool_results(
            [ToolResult(ok=False, tool="t", error_code="E", error_message="忙")], "t"
        ).verdict.rule
    )
    rules.add(guard.check_confidence(0.0, "unknown").verdict.rule)
    rules.add(guard.check_tool_call("my_loans", {"readerId": 1}, session_reader_id=None, confirmed=True).verdict.rule)
    assert len(rules) >= 10, f"规则不足 10 条：{sorted(rules)}"
