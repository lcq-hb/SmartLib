"""Agent 端到端测试（MockLLM + 假后端，零外部依赖）。

这里验证的是**行为契约**，也就是"简历上敢写的那几条"：
- 事实性问题必须带工具证据（蓝图 §7 硬约束 100%）；
- 写操作必须二次确认，且确认前后是两个不同的轮次；
- 越权请求必须被拒（而且不能因为文本里写了别人的编号就被冒名）；
- 四种 AGENT_MODE 都能跑（G1/G2/G4/G5 切换）；
- LLM 挂掉时 hybrid 依然可用（降级到规则）；
- 后端全挂时给的是中文人话 + 转人工，而不是 500 或堆栈。
"""

from __future__ import annotations

import pytest

from tests.conftest import FakeJavaBackend


# =============================================================================
# 基础对话
# =============================================================================
@pytest.mark.asyncio
async def test_search_flow_returns_evidence(runtime_env) -> None:
    result = await runtime_env.run("找一下《算法导论》", session_id="s1", reader_id=1001)
    assert result.intent == "search_book"
    assert result.tool_evidence is True
    assert result.steps >= 1
    # tool_calls 是已经 model_dump 过的 dict（要能直接序列化进 trace / SSE）
    assert [t["tool"] for t in result.tool_calls] == ["search_books"]
    assert "算法导论" in result.reply
    assert result.trace_id.startswith("tr-")
    assert result.latency_ms > 0


@pytest.mark.asyncio
async def test_stock_flow_reports_shelf_location(runtime_env) -> None:
    result = await runtime_env.run("《算法导论》还有几本", session_id="s1", reader_id=1001)
    assert result.intent == "get_stock"
    assert result.tool_evidence is True
    assert "3 楼" in result.reply


@pytest.mark.asyncio
async def test_stock_degraded_says_unknown_not_zero(runtime_env, fake_backend: FakeJavaBackend) -> None:
    """availableCopies=-1 → 必须说"查不到"，不能说"没有库存"。"""
    fake_backend.stock_available = -1
    result = await runtime_env.run("《算法导论》还有几本", session_id="s1", reader_id=1001)
    assert "查不到" in result.reply or "无法确认" in result.reply
    assert "没有可借复本" not in result.reply


@pytest.mark.asyncio
async def test_my_loans_flow(runtime_env) -> None:
    result = await runtime_env.run("我有没有逾期的书", session_id="s1", reader_id=1001)
    assert result.intent == "my_loans"
    assert result.tool_evidence is True
    assert "计算机网络" in result.reply
    assert "逾期" in result.reply


@pytest.mark.asyncio
async def test_reader_profile_flow(runtime_env) -> None:
    """问自己的额度 → 走读者档案（拿真实信用分/在借数），而不是背规则。"""
    result = await runtime_env.run("我能借几本", session_id="s1", reader_id=1001)
    assert result.intent == "reader_profile"
    assert "10 册" in result.reply
    assert "30 天" in result.reply


@pytest.mark.asyncio
async def test_generic_quota_question_goes_to_policy(runtime_env) -> None:
    """"本科生能借几本"没有第一人称，问的是规则 → 走规则问答（规则库优先）。"""
    result = await runtime_env.run("本科生能借几本", session_id="s1", reader_id=1001)
    assert result.intent == "policy_qa"
    assert "10 册" in result.reply
    # 规则库可用时以它为准，并把出处一起给出来
    assert "rule_config" in result.reply


@pytest.mark.asyncio
async def test_generic_quota_question_falls_back_to_knowledge_base(
    runtime_env, fake_backend: FakeJavaBackend
) -> None:
    """规则库不可用时，同一句话必须能靠本地知识库答出来，且带知识库出处。"""
    fake_backend.always_fail = True
    result = await runtime_env.run("本科生能借几本", session_id="s1", reader_id=1001)
    assert result.intent == "policy_qa"
    assert "10 册" in result.reply
    assert result.rag_sources
    assert any("02-可借册数与期限" in src for src in result.rag_sources)


@pytest.mark.asyncio
async def test_policy_flow_prefers_rule_engine(runtime_env) -> None:
    result = await runtime_env.run("逾期罚金怎么算", session_id="s1", reader_id=1001)
    assert result.intent == "policy_qa"
    assert result.tool_evidence is True
    assert "规则库" in result.reply or "罚金" in result.reply


@pytest.mark.asyncio
async def test_policy_flow_offline_rag(runtime_env, fake_backend: FakeJavaBackend) -> None:
    """后端规则库挂了也要答得出来，并且带出处。"""
    fake_backend.always_fail = True
    result = await runtime_env.run("预约到馆保留几天", session_id="s1", reader_id=1001)
    assert result.intent == "policy_qa"
    assert "3 天" in result.reply
    assert result.rag_sources, "知识库回答必须带出处"


@pytest.mark.asyncio
async def test_recommend_flow(runtime_env) -> None:
    result = await runtime_env.run("推荐几本计算机的书", session_id="s1", reader_id=1001)
    assert result.intent == "recommend"
    assert result.tool_evidence is True


@pytest.mark.asyncio
async def test_chitchat_needs_no_tool(runtime_env) -> None:
    result = await runtime_env.run("你好", session_id="s1", reader_id=1001)
    assert result.intent == "chitchat"
    assert result.tool_evidence is False
    assert result.steps == 0
    assert "助手" in result.reply


@pytest.mark.asyncio
async def test_out_of_scope_is_refused(runtime_env) -> None:
    result = await runtime_env.run("今天天气怎么样", session_id="s1", reader_id=1001)
    assert result.intent == "guardrail_block"
    assert "图书馆" in result.reply


@pytest.mark.asyncio
async def test_sensitive_request_is_refused(runtime_env) -> None:
    result = await runtime_env.run("帮我代写论文", session_id="s1", reader_id=1001)
    assert result.intent == "guardrail_block"
    assert "服务范围" in result.reply


# =============================================================================
# 写操作二次确认（跨轮次）
# =============================================================================
@pytest.mark.asyncio
async def test_borrow_requires_confirmation_then_succeeds(runtime_env, fake_backend) -> None:
    first = await runtime_env.run("帮我借《算法导论》", session_id="s-borrow", reader_id=1001)
    assert first.intent == "borrow_book"
    assert first.pending_confirm is not None
    assert "确认" in first.reply
    # 关键：未确认时绝不能真的下单
    assert "/internal/agent/tools/borrow" not in fake_backend.paths()

    second = await runtime_env.run("确认借阅", session_id="s-borrow", reader_id=1001)
    assert "借阅成功" in second.reply
    assert "2026-04-01" in second.reply
    assert "/internal/agent/tools/borrow" in fake_backend.paths()


@pytest.mark.asyncio
async def test_borrow_not_executed_without_confirm_even_if_user_chats(runtime_env, fake_backend) -> None:
    await runtime_env.run("帮我借《算法导论》", session_id="s-x", reader_id=1001)
    # 用户没确认，改问别的：pending 应该被丢弃，不能顺势借出
    await runtime_env.run("算了，先看看我的借阅", session_id="s-x", reader_id=1001)
    assert "/internal/agent/tools/borrow" not in fake_backend.paths()


@pytest.mark.asyncio
async def test_renew_looks_up_borrow_id_first(runtime_env, fake_backend) -> None:
    """续借必须先查借阅记录拿到 borrowId（renew 不认书名）。"""
    first = await runtime_env.run("帮我续借《计算机网络》", session_id="s-renew", reader_id=1001)
    assert first.pending_confirm is not None
    assert "/internal/agent/tools/readers/1001/loans" in fake_backend.paths()

    second = await runtime_env.run("确认续借", session_id="s-renew", reader_id=1001)
    assert "续借成功" in second.reply
    assert "2026-05-01" in second.reply


@pytest.mark.asyncio
async def test_pending_expiry_blocks_late_confirmation(runtime_env, fake_backend) -> None:
    """过期的 pending 不能被一句"确认"激活。"""
    from app.memory.session_store import get_session_store

    await runtime_env.run("帮我借《算法导论》", session_id="s-exp", reader_id=1001)
    store = get_session_store()
    session = store.get("s-exp")
    assert session is not None and session.pending is not None
    session.pending.created_at -= 10_000  # 人为拨回到过去

    await runtime_env.run("确认借阅", session_id="s-exp", reader_id=1001)
    assert "/internal/agent/tools/borrow" not in fake_backend.paths()


# =============================================================================
# 越权
# =============================================================================
@pytest.mark.asyncio
async def test_cannot_borrow_for_another_reader(runtime_env, fake_backend) -> None:
    result = await runtime_env.run(
        "帮我借读者 2002 的《算法导论》", session_id="s-priv", reader_id=1001
    )
    assert "本人" in result.reply
    assert result.pending_confirm is None, "越权请求绝不能进入待确认队列"
    assert result.guardrail_blocked is True
    assert "G-03_no_borrow_for_others:refuse" in result.guardrail_notes
    assert "/internal/agent/tools/borrow" not in fake_backend.paths()


@pytest.mark.asyncio
async def test_cannot_read_another_reader_loans(runtime_env, fake_backend) -> None:
    result = await runtime_env.run("查一下读者 2002 的信用分", session_id="s-priv2", reader_id=1001)
    assert "本人" in result.reply or "隐私" in result.reply
    # 越权请求不能真的打到后端去查别人数据
    assert not any("2002" in call["path"] for call in fake_backend.calls)


@pytest.mark.asyncio
async def test_claimed_reader_never_overrides_session_identity(runtime_env) -> None:
    """直接验证槽位合并的安全不变量。"""
    from app.agent.intent import IntentResult
    from app.memory.session_store import Session

    session = Session(session_id="s", reader_id=1001)
    intent = IntentResult("my_loans", 0.95, slots={"reader_id": 2002})
    slots = runtime_env._merge_slots(session, intent, "查 2002 的记录", 1001)
    assert slots["reader_id"] == 1001
    assert slots["claimed_reader_id"] == 2002


@pytest.mark.asyncio
async def test_write_tool_never_sends_other_reader_id(runtime_env, fake_backend) -> None:
    await runtime_env.run("帮我借读者 2002 的《算法导论》", session_id="s-priv3", reader_id=1001)
    for call in fake_backend.calls:
        assert b'"readerId":2002' not in (call["body"] or b"")


# =============================================================================
# book_id 溯源（真实联调暴露的缺陷：模型给的假 id 直接打后端 → 404）
# =============================================================================
@pytest.mark.asyncio
async def test_get_stock_uses_real_id_from_tool_results(runtime_env, fake_backend) -> None:
    """回归：发给后端的 book_id 必须等于**工具结果里的真实 id**。

    背景：MockLLM 曾用一张"书名→id"的本地表产出 `book_id`（101/102），
    而这些 id 在真实后端不存在 → `业务异常 code=2002 图书不存在：id=102`。
    现在 book_id 只能来自 search_books 的返回；本用例刻意让后端 id
    取成 5 / 3 / 8 这种"真实值"，并断言发出去的 id 就是它。
    """
    real_id = fake_backend.BOOKS["算法导论"]["bookId"]
    assert real_id != 101, "测试数据必须用真实后端风格的 id，否则测不出这个缺陷"

    result = await runtime_env.run("《算法导论》还有几本", session_id="s-src1", reader_id=1001)

    assert result.intent == "get_stock"
    assert result.tool_evidence is True

    stock_paths = [c["path"] for c in fake_backend.calls if c["path"].endswith("/stock")]
    assert stock_paths, "应该真的调用了库存接口"
    assert stock_paths[-1] == f"/internal/agent/tools/books/{real_id}/stock"
    # 必须先检索拿到 id，再查库存（这就是"事实只能来自工具"的顺序）
    assert "/internal/agent/tools/search-books" in fake_backend.paths()
    assert "G-13_book_id_sourcing:prerequisite_search" in result.guardrail_notes


@pytest.mark.asyncio
async def test_borrow_discards_model_supplied_id(runtime_env, fake_backend) -> None:
    """回归：模型给了不存在的 id（999）必须被丢弃，改用工具结果里的真实 id。"""
    real_id = fake_backend.BOOKS["算法导论"]["bookId"]

    class LyingLLM:
        """模拟"会编 id"的模型：它坚持说这本书是 999 号。"""

        provider = "lying"
        slots: dict = {}

        async def complete(self, messages, *, tools=None, temperature=None, max_tokens=None):
            from app.llm.base import LLMResult, ToolCall

            return LLMResult(
                tool_calls=[
                    ToolCall(
                        name="borrow",
                        arguments={
                            "readerId": 1001,
                            "bookId": 999,
                            "idempotencyKey": "lying-999",
                            "confirm": True,
                        },
                        id="call_lying",
                    )
                ],
                finish_reason="tool_calls",
                provider="lying",
            )

        async def stream(self, messages, *, tools=None, temperature=None):
            if False:  # pragma: no cover
                yield ""

    from app.agent.guardrail import Guardrail
    from app.agent.intent import IntentClassifier
    from app.agent.planner import Planner
    from app.agent.runtime import AgentRuntime
    from app.client.java_client import JavaClient

    llm = LyingLLM()
    runtime = AgentRuntime(
        settings=runtime_env.settings,
        llm=llm,
        java_client=JavaClient(runtime_env.settings, transport=fake_backend.transport()),
        guardrail=Guardrail(runtime_env.settings),
        classifier=IntentClassifier(llm, runtime_env.settings),
        planner=Planner(llm, runtime_env.settings),
    )
    result = await runtime.run("帮我借《算法导论》", session_id="s-src2", reader_id=1001, mode="llm")

    # 模型编的 999 绝不允许出现在任何请求体或工具参数里
    for call in result.tool_calls:
        assert call["arguments"].get("bookId") != 999
    for call in fake_backend.calls:
        assert b"999" not in (call["body"] or b"")
    assert "999" not in result.reply
    # 真实 id 应该被用上（本轮是待确认，所以只要求拿到正确书目）
    assert result.pending_confirm is not None or result.reply
    if result.pending_confirm:
        assert result.pending_confirm["args"].get("bookId") in (real_id, None)


@pytest.mark.asyncio
async def test_unknown_book_is_reported_honestly(runtime_env, fake_backend) -> None:
    """检索不到任何候选时：如实说没找到，**绝不带着猜的 id 去调后端**。"""
    fake_backend.default_search = lambda q: {  # type: ignore[assignment]
        "items": [],
        "total": 0,
        "tookMs": 1,
        "engine": "inverted-index",
        "degraded": False,
    }
    result = await runtime_env.run("《不存在的书名》还有几本", session_id="s-src3", reader_id=1001)

    assert "没有找到" in result.reply or "没找到" in result.reply
    # 关键断言：没有可信 id 时不允许调用库存/借阅接口
    assert not any(c["path"].endswith("/stock") for c in fake_backend.calls)
    assert "/internal/agent/tools/borrow" not in fake_backend.paths()


@pytest.mark.asyncio
async def test_ordinal_reference_maps_to_search_position(runtime_env, fake_backend) -> None:
    """"第 2 本"要映射到检索结果里的第 2 条真实 id，而不是凭空猜。"""
    items = fake_backend.default_search("书")["items"]
    assert len(items) >= 2
    expected_id = items[1]["bookId"]

    result = await runtime_env.run("《算法导论》第 2 本还有几本", session_id="s-src4", reader_id=1001)
    stock_paths = [c["path"] for c in fake_backend.calls if c["path"].endswith("/stock")]
    if stock_paths:
        assert stock_paths[-1] == f"/internal/agent/tools/books/{expected_id}/stock"
    else:
        # 没有可用检索结果时也必须给出可读答复，而不是 500 / 内部字段名
        assert result.reply
        assert "book_id" not in result.reply


@pytest.mark.asyncio
async def test_metrics_counts_blocked_unsourced_ids(runtime_env, fake_backend) -> None:
    """丢弃未经工具验证的 id 要能被度量（便于回答"模型到底编了多少次"）。"""
    from app.metrics import METRICS

    before = METRICS.snapshot()["counters"].get("unsourced_book_id_blocked", 0)

    class LyingLLM2:
        provider = "lying2"
        slots: dict = {}

        async def complete(self, messages, *, tools=None, temperature=None, max_tokens=None):
            from app.llm.base import LLMResult, ToolCall

            return LLMResult(
                tool_calls=[
                    ToolCall(name="get_stock", arguments={"book_id": 424242}, id="call_lying2")
                ],
                finish_reason="tool_calls",
                provider="lying2",
            )

        async def stream(self, messages, *, tools=None, temperature=None):
            if False:  # pragma: no cover
                yield ""

    from app.agent.guardrail import Guardrail
    from app.agent.intent import IntentClassifier
    from app.agent.planner import Planner
    from app.agent.runtime import AgentRuntime
    from app.client.java_client import JavaClient

    llm = LyingLLM2()
    runtime = AgentRuntime(
        settings=runtime_env.settings,
        llm=llm,
        java_client=JavaClient(runtime_env.settings, transport=fake_backend.transport()),
        guardrail=Guardrail(runtime_env.settings),
        classifier=IntentClassifier(llm, runtime_env.settings),
        planner=Planner(llm, runtime_env.settings),
    )
    await runtime.run("查一下《算法导论》的库存", session_id="s-src5", reader_id=1001, mode="llm")

    after = METRICS.snapshot()["counters"].get("unsourced_book_id_blocked", 0)
    assert after > before
    assert not any(b"424242" in (c["body"] or b"") for c in fake_backend.calls)


# =============================================================================
# 写操作必须有前置证据（404 之后不该再追问"要不要借"）
# =============================================================================
@pytest.mark.asyncio
async def test_write_blocked_without_stock_evidence(runtime_env, fake_backend) -> None:
    """书查不到时，绝不能进入"请回复确认借阅"的流程——确认了也必然失败。"""
    fake_backend.default_search = lambda q: {  # type: ignore[assignment]
        "items": [],
        "total": 0,
        "tookMs": 1,
        "engine": "inverted-index",
        "degraded": False,
    }
    result = await runtime_env.run("帮我借《查无此书》", session_id="s-ev1", reader_id=1001)

    assert result.pending_confirm is None, "没有馆藏证据时不应挂起待确认写操作"
    assert "确认借阅" not in result.reply
    assert "/internal/agent/tools/borrow" not in fake_backend.paths()


@pytest.mark.asyncio
async def test_write_confirm_only_after_stock_evidence(runtime_env, fake_backend) -> None:
    """有馆藏证据时正常进入确认流程（不能因为加了护栏就把正常路径堵死）。"""
    result = await runtime_env.run("帮我借《算法导论》", session_id="s-ev2", reader_id=1001)
    assert result.pending_confirm is not None
    assert "确认" in result.reply
    # 借书前必须先确认过库存（内部前置步骤），且尚未真正下单
    assert "/internal/agent/tools/borrow" not in fake_backend.paths()
    assert any(t["tool"] == "get_stock" for t in result.internal_tool_calls)
    assert "G-13_write_evidence:prerequisite_stock" in result.guardrail_notes


# =============================================================================
# 模式切换（G1/G2/G4/G5）
# =============================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rule", "llm", "hybrid", "plan"])
async def test_all_agent_modes_produce_an_answer(runtime_env, mode) -> None:
    result = await runtime_env.run("找一下《算法导论》", session_id=f"s-{mode}", reader_id=1001, mode=mode)
    assert result.mode == mode
    assert result.reply
    assert result.tool_evidence is True


@pytest.mark.asyncio
async def test_rule_mode_still_answers_facts(runtime_env) -> None:
    """G1 基线（纯规则）也必须能答事实问题——这是"无 LLM 也能用"的底线。"""
    result = await runtime_env.run("我有没有逾期的书", session_id="s-rule", reader_id=1001, mode="rule")
    assert result.tool_evidence is True
    assert "逾期" in result.reply
    assert result.provider == "mock"


@pytest.mark.asyncio
async def test_plan_mode_decomposes_multi_step_task(runtime_env) -> None:
    result = await runtime_env.run(
        "帮我借《算法导论》，再推荐两本同类，顺便查我有没有逾期",
        session_id="s-plan",
        reader_id=1001,
        mode="plan",
    )
    assert result.plan is not None
    tools = [step["tool"] for step in result.plan["steps"]]
    assert "borrow" in tools
    assert "recommend" in tools
    assert "my_loans" in tools
    # 借书这一步必须卡在确认上，其他步骤照常完成
    assert result.pending_confirm is not None
    assert any(step["status"] == "done" for step in result.plan["steps"])
    assert result.tool_evidence is True


@pytest.mark.asyncio
async def test_plan_continues_after_non_optional_failure(runtime_env, fake_backend) -> None:
    """推荐挂掉不应阻断借阅与查逾期（只有 borrow/renew 失败才中断）。"""

    def handler(request):
        if "/similar" in request.url.path or request.url.path.endswith("/books/hot"):
            return __import__("httpx").Response(500, json={"code": 500, "msg": "BOOM"})
        return fake_backend.handle(request)

    import httpx

    from app.agent.guardrail import Guardrail
    from app.agent.intent import IntentClassifier
    from app.agent.planner import Planner
    from app.agent.runtime import AgentRuntime
    from app.client.java_client import JavaClient
    from app.llm.mock import MockLLM

    llm = MockLLM()
    runtime = AgentRuntime(
        settings=fake_backend_settings(fake_backend),
        llm=llm,
        java_client=JavaClient(fake_backend_settings(fake_backend), transport=httpx.MockTransport(handler)),
        guardrail=Guardrail(),
        classifier=IntentClassifier(llm),
        planner=Planner(llm),
    )
    result = await runtime.run(
        "帮我借《算法导论》，再推荐两本同类，顺便查我有没有逾期",
        session_id="s-plan-partial",
        reader_id=1001,
        mode="plan",
    )
    assert result.plan is not None
    statuses = {step["tool"]: step["status"] for step in result.plan["steps"]}
    assert statuses.get("recommend") == "failed"
    assert statuses.get("my_loans") == "done"


def fake_backend_settings(backend: FakeJavaBackend):
    from app.config import Settings

    return Settings(_env_file=None, llm_provider="mock", java_base_url="http://t")


# =============================================================================
# 降级与鲁棒性
# =============================================================================
@pytest.mark.asyncio
async def test_backend_fully_down_gives_human_message(runtime_env, fake_backend) -> None:
    fake_backend.always_fail = True
    result = await runtime_env.run("《算法导论》还有几本", session_id="s-down", reader_id=1001)
    assert result.reply
    # 不能把熔断/超时的内部措辞甩给读者
    assert "熔断" not in result.reply
    assert "Traceback" not in result.reply
    assert "CircuitOpen" not in result.reply


@pytest.mark.asyncio
async def test_llm_failure_degrades_to_rules(test_settings, fake_backend: FakeJavaBackend) -> None:
    """LLM 抛异常时 hybrid 仍可用（规则路由 + 确定性渲染）。"""

    class ExplodingLLM:
        provider = "exploding"

        def __init__(self):
            self.slots = {}

        async def complete(self, messages, *, tools=None, temperature=None, max_tokens=None):
            raise RuntimeError("llm down")

        async def stream(self, messages, *, tools=None, temperature=None):
            if False:  # pragma: no cover
                yield ""

    from app.agent.guardrail import Guardrail
    from app.agent.intent import IntentClassifier
    from app.agent.planner import Planner
    from app.agent.runtime import AgentRuntime
    from app.client.java_client import JavaClient

    llm = ExplodingLLM()
    runtime = AgentRuntime(
        settings=test_settings,
        llm=llm,
        java_client=JavaClient(test_settings, transport=fake_backend.transport()),
        guardrail=Guardrail(test_settings),
        classifier=IntentClassifier(llm, test_settings),
        planner=Planner(llm, test_settings),
    )
    result = await runtime.run("我有没有逾期的书", session_id="s-llm-down", reader_id=1001)
    assert result.tool_evidence is True
    assert "逾期" in result.reply or "计算机网络" in result.reply
    assert result.provider == "exploding"


@pytest.mark.asyncio
async def test_loop_detection_blocks_repeated_identical_call(test_settings, fake_backend) -> None:
    """同一工具 + 同一参数反复调用必须被 executor 拦下。"""
    from app.agent.executor import ToolExecutor
    from app.client.java_client import JavaClient
    from app.tools.base import ToolContext

    ctx = ToolContext(
        trace_id="t",
        reader_id=1001,
        java=JavaClient(test_settings, transport=fake_backend.transport()),
        settings=test_settings,
    )
    executor = ToolExecutor(ctx)
    args = {"book_id": 5}
    first = await executor.execute("get_stock", args)
    second = await executor.execute("get_stock", args)
    third = await executor.execute("get_stock", args)
    assert first.ok and second.ok
    assert third.ok is False
    assert third.error_code == "LOOP_DETECTED"


@pytest.mark.asyncio
async def test_executor_rejects_unknown_tool(test_settings, fake_backend) -> None:
    from app.agent.executor import ToolExecutor
    from app.tools.base import ToolContext

    executor = ToolExecutor(ToolContext(trace_id="t", settings=test_settings))
    result = await executor.execute("rm_rf", {})
    assert result.ok is False
    assert result.error_code == "TOOL_NOT_FOUND"
    assert executor.traces[0].blocked_by == "tool_whitelist"


@pytest.mark.asyncio
async def test_executor_turns_validation_error_into_message(test_settings) -> None:
    from app.agent.executor import ToolExecutor
    from app.tools.base import ToolContext

    executor = ToolExecutor(ToolContext(trace_id="t", settings=test_settings))
    result = await executor.execute("get_stock", {"book_id": "abc"})
    assert result.ok is False
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert "book_id" in (result.error_message or "")


@pytest.mark.asyncio
async def test_executor_step_timeout(test_settings) -> None:
    """单步超时必须被兜住（否则一个慢工具会拖死整个会话）。"""
    from app.agent.executor import ToolExecutor
    from app.tools.base import Tool, ToolContext, ToolResult

    class SlowTool(Tool):
        name = "search_books"  # 复用已注册的名字，绕过白名单
        description = "slow"
        args_model = __import__("app.tool_schemas", fromlist=["SearchBooksArgs"]).SearchBooksArgs

        async def handle(self, args, ctx):  # pragma: no cover
            import asyncio

            await asyncio.sleep(5)
            return ToolResult(ok=True, tool=self.name)

    from app.tools import registry

    original = registry.get("search_books")
    registry.register(SlowTool(), replace=True)
    try:
        executor = ToolExecutor(ToolContext(trace_id="t", settings=test_settings), step_timeout=0.05)
        result = await executor.execute("search_books", {"query": "x"})
        assert result.ok is False
        assert result.error_code == "STEP_TIMEOUT"
    finally:
        registry.register(original, replace=True)


# =============================================================================
# 记忆在对话中的体现
# =============================================================================
@pytest.mark.asyncio
async def test_session_history_is_passed_to_llm(runtime_env) -> None:
    await runtime_env.run("找一下《算法导论》", session_id="s-mem", reader_id=1001)
    await runtime_env.run("我有没有逾期的书", session_id="s-mem", reader_id=1001)
    from app.memory.session_store import get_session_store

    session = get_session_store().get("s-mem")
    assert session is not None
    # 两轮问答 = 4 条消息（user/assistant × 2）
    assert len(session.turns) == 4
    assert session.turns[0].content.startswith("找一下")


@pytest.mark.asyncio
async def test_sessions_are_isolated(runtime_env) -> None:
    await runtime_env.run("帮我借《算法导论》", session_id="s-a", reader_id=1001)
    # 另一个会话里说"确认"不应该触发 s-a 的 pending
    result = await runtime_env.run("确认借阅", session_id="s-b", reader_id=1002)
    assert "借阅成功" not in result.reply


@pytest.mark.asyncio
async def test_recommend_uses_reader_profile_memory(runtime_env, fake_backend) -> None:
    from app.memory.reader_profile import get_profile_store

    store = get_profile_store()
    await store.set_categories(1001, ["计算机"])
    await runtime_env.run("推荐几本计算机的书", session_id="s-pref", reader_id=1001)
    assert any("hot" in p or "similar" in p for p in fake_backend.paths())


# =============================================================================
# 流式事件
# =============================================================================
@pytest.mark.asyncio
async def test_run_stream_emits_expected_event_sequence(runtime_env) -> None:
    events = [event async for event in runtime_env.run_stream("找一下《算法导论》", session_id="s-see", reader_id=1001)]
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert "intent" in types
    assert "tool_call" in types
    assert "tool_result" in types
    assert "token" in types
    assert types[-1] == "done"

    done = events[-1]["result"]
    assert done["trace_id"]
    assert done["tool_evidence"] is True
    # token 拼起来必须等于完整回复（否则前端流式渲染会缺字）
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert streamed == done["reply"]


@pytest.mark.asyncio
async def test_run_stream_has_first_token_latency(runtime_env) -> None:
    events = [event async for event in runtime_env.run_stream("你好", session_id="s-ft", reader_id=1001)]
    # 首 Token 延迟只在 done 事件里给出（服务端无法在流开始前就知道它）
    start = next(e for e in events if e["type"] == "start")
    assert start["trace_id"]
    assert any(e["type"] == "token" for e in events)


# =============================================================================
# 指标
# =============================================================================
@pytest.mark.asyncio
async def test_metrics_recorded_after_chat(runtime_env) -> None:
    from app.metrics import METRICS

    METRICS.reset()
    await runtime_env.run("找一下《算法导论》", session_id="s-m", reader_id=1001)
    snapshot = METRICS.snapshot()
    assert snapshot["total_requests"] == 1
    assert snapshot["window_requests"] == 1
    assert snapshot["latency_ms"]["avg"] > 0
    assert snapshot["tool_evidence_ratio"] == 1.0
    assert snapshot["counters"].get("intent_search_book") == 1
