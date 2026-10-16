"""意图识别测试。

覆盖三件事：
1. 规则分类在**易混意图**上的准确性（咨询 vs 写操作、库存 vs 规则、检索 vs 推荐）；
2. 置信度分档是否符合设计（强特征 0.95 / 多弱特征 0.75 / 单弱特征 + 疑问 0.75）；
3. LLM 兜底路径：MockLLM 给出 tool_calls 时也能反推出意图，且 LLM 挂掉不影响规则路径。
"""

from __future__ import annotations

import pytest

from app.agent.intent import (
    INTENTS,
    IntentClassifier,
    IntentResult,
    classify_by_rules,
    parse_intent_json,
)
from app.llm.base import ChatMessage, LLMResult, ToolCall

#: (用户输入, 期望意图)。这些用例是"规则路由的质量基线"，
#: 每条都对应一个真实的易错点，注释说明它为什么容易错。
ROUTING_CASES: list[tuple[str, str]] = [
    # 写操作 vs 规则咨询（最容易把"怎么借"当成"帮我借"）
    ("帮我借《算法导论》", "borrow_book"),
    ("我要借《高等数学》", "borrow_book"),
    ("怎么借书", "policy_qa"),
    ("借阅规则是什么", "policy_qa"),
    ("帮我续借《计算机网络》", "renew_book"),
    ("怎么续借", "policy_qa"),
    ("能续借几次", "policy_qa"),
    ("我的书快到期了能延期吗", "renew_book"),
    ("续借规则是什么", "policy_qa"),
    # 个人数据 vs 规则
    ("我借了哪些书", "my_loans"),
    ("我有没有逾期的书", "my_loans"),
    ("我的借阅记录", "my_loans"),
    ("逾期罚金怎么算", "policy_qa"),
    ("我的信用分是多少", "reader_profile"),
    ("我能借几本", "reader_profile"),
    # "研究生一次能借多少册"没有第一人称 → 问的是**规则**（policy_qa）；
    # 有第一人称的"我能借几本"才查读者档案。这是一条刻意保留的歧义分界线。
    ("研究生一次能借多少册", "policy_qa"),
    ("我的借阅额度还剩多少", "reader_profile"),
    # 库存 vs 规则（"在几楼"是知识，"还有几本"是实时数据）
    ("《算法导论》还有几本", "get_stock"),
    ("《机器学习》可借吗", "get_stock"),
    ("这本书在馆吗", "get_stock"),
    ("计算机网络在哪个书架", "get_stock"),
    ("计算机类的书在几楼", "policy_qa"),
    ("图书馆几点开门", "policy_qa"),
    ("预约到馆保留几天", "policy_qa"),
    # 检索 vs 推荐（书名号是弱特征，不能压过"类似/推荐"）
    ("找一下《算法导论》", "search_book"),
    ("我想找谢希仁写的计算机网络", "search_book"),
    ("帮我找找机器学习相关的书", "search_book"),
    ("类似《算法导论》的还有哪些", "recommend"),
    ("推荐几本机器学习的书", "recommend"),
    ("有什么好书推荐", "recommend"),
    # 闲聊
    ("你好", "chitchat"),
    ("谢谢", "chitchat"),
    ("你能做什么", "chitchat"),
]


@pytest.mark.parametrize("message,expected", ROUTING_CASES)
def test_rule_routing(message: str, expected: str) -> None:
    result = classify_by_rules(message)
    assert result is not None, f"规则完全没命中：{message}"
    assert result.intent == expected, f"{message} → {result.intent}（期望 {expected}）"


def test_all_intents_declared_in_table() -> None:
    """ROUTING_CASES 里出现的意图必须都在 INTENTS 里声明过。"""
    for _message, intent in ROUTING_CASES:
        assert intent in INTENTS


def test_strong_match_confidence_is_high() -> None:
    result = classify_by_rules("帮我借《算法导论》")
    assert result is not None
    assert result.confidence == pytest.approx(0.95)
    assert result.source == "rule"
    assert result.matched and result.matched[0].startswith("strong:")


def test_weak_match_confidence_lower_than_strong() -> None:
    strong = classify_by_rules("帮我借《算法导论》")
    weak = classify_by_rules("图书馆几点开门")
    assert strong is not None and weak is not None
    assert weak.confidence < strong.confidence


def test_unknown_message_returns_none() -> None:
    assert classify_by_rules("今天天气怎么样") is None
    assert classify_by_rules("") is None


def test_slots_extraction() -> None:
    result = classify_by_rules("帮我借《算法导论》")
    assert result is not None
    assert result.slots.get("book_title") == "算法导论"
    # 未确认时 confirmed 必须为 False —— 这是写操作二次确认的第一道依据
    assert result.slots.get("confirmed") is False

    confirmed = classify_by_rules("确认借阅《算法导论》")
    assert confirmed is not None
    assert confirmed.slots.get("confirmed") is True


def test_reader_id_slot_extracted_but_not_trusted_by_runtime() -> None:
    """意图层可以抽出编号，但 runtime 不会用它覆盖会话身份（见 test_agent_e2e）。"""
    result = classify_by_rules("帮我借读者 2002 的《算法导论》")
    assert result is not None
    assert result.slots.get("reader_id") == 2002


def test_intent_result_exposes_tool_mapping() -> None:
    result = classify_by_rules("我借了哪些书")
    assert result is not None
    assert result.tool == "my_loans"
    assert result.needs_reader is True
    assert result.is_write is False

    write = classify_by_rules("帮我借《算法导论》")
    assert write is not None and write.is_write is True


def test_parse_intent_json_tolerates_markdown_fence() -> None:
    raw = '```json\n{"intent":"policy_qa","confidence":0.8,"reason":"问规则"}\n```'
    payload = parse_intent_json(raw)
    assert payload["intent"] == "policy_qa"


def test_parse_intent_json_returns_empty_on_garbage() -> None:
    assert parse_intent_json("我觉得这大概是找书吧") == {}
    assert parse_intent_json("") == {}


@pytest.mark.asyncio
async def test_llm_fallback_uses_tool_choice_as_intent(test_settings) -> None:
    """MockLLM 不产出 JSON，但它的 tool_calls 也是有效意图信号。"""

    class ToolCallingLLM:
        provider = "fake"

        async def complete(self, messages, *, tools=None, temperature=None, max_tokens=None):
            return LLMResult(
                tool_calls=[ToolCall(name="search_books", arguments={"query": "x"}, id="c1")],
                finish_reason="tool_calls",
                provider="fake",
            )

        async def stream(self, messages, *, tools=None, temperature=None):
            if False:  # pragma: no cover
                yield ""

    classifier = IntentClassifier(ToolCallingLLM(), test_settings)  # type: ignore[arg-type]
    # 用一条规则命中不了的话，强制走 LLM 兜底
    result = await classifier.classify("嗯那个什么来着", mode="hybrid")
    assert result.intent == "search_book"
    assert result.source == "llm"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_rules(test_settings) -> None:
    """LLM 抛异常时，低置信度句子也不能把问答打断，必须退回规则/unknown。"""

    class BrokenLLM:
        provider = "broken"

        async def complete(self, messages, *, tools=None, temperature=None, max_tokens=None):
            raise RuntimeError("boom")

        async def stream(self, messages, *, tools=None, temperature=None):
            if False:  # pragma: no cover
                yield ""

    classifier = IntentClassifier(BrokenLLM(), test_settings)  # type: ignore[arg-type]
    result = await classifier.classify("嗯那个什么来着", mode="hybrid")
    assert result.intent in INTENTS
    assert result.source in ("fallback", "rule")


@pytest.mark.asyncio
async def test_rule_mode_never_calls_llm(test_settings) -> None:
    """AGENT_MODE=rule 时必须完全不调 LLM（这是 LLM 故障时的应急开关）。"""

    class ExplodingLLM:
        provider = "exploding"
        calls = 0

        async def complete(self, messages, *, tools=None, temperature=None, max_tokens=None):
            ExplodingLLM.calls += 1
            raise AssertionError("rule 模式不应该调用 LLM")

        async def stream(self, messages, *, tools=None, temperature=None):
            if False:  # pragma: no cover
                yield ""

    classifier = IntentClassifier(ExplodingLLM(), test_settings)  # type: ignore[arg-type]
    await classifier.classify("嗯那个什么来着", mode="rule")
    assert ExplodingLLM.calls == 0


@pytest.mark.asyncio
async def test_high_confidence_rule_skips_llm(test_settings) -> None:
    """高置信度规则命中时不应浪费一次 LLM 调用（这是 hybrid 模式的省钱关键）。"""

    class CountingLLM:
        provider = "counting"
        calls = 0

        async def complete(self, messages, *, tools=None, temperature=None, max_tokens=None):
            CountingLLM.calls += 1
            return LLMResult(content='{"intent":"chitchat","confidence":0.9}')

        async def stream(self, messages, *, tools=None, temperature=None):
            if False:  # pragma: no cover
                yield ""

    classifier = IntentClassifier(CountingLLM(), test_settings)  # type: ignore[arg-type]
    result = await classifier.classify("帮我借《算法导论》", mode="hybrid")
    assert result.intent == "borrow_book"
    assert CountingLLM.calls == 0


def test_intent_result_to_dict_shape() -> None:
    result = IntentResult("policy_qa", 0.95, slots={"topic": "x"})
    payload = result.to_dict()
    assert payload["label"] == "续借规则咨询" or payload["label"] == INTENTS["policy_qa"]["label"]
    assert payload["tool"] == "policy_qa"
    assert payload["confidence"] == 0.95
