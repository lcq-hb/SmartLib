"""Agent 主循环：ReAct 与 Plan-and-Execute 双模式（蓝图 §4.3 / §6.4）。

四种 AGENT_MODE 与蓝图 G1~G5 的对应关系：
    rule   → 规则直答（G1 基线：不调 LLM，纯规则 + 工具；最稳最快，用于对比）
    llm    → 纯 LLM 决策（G2：每次都由 LLM 决定调哪个工具，最灵活但最贵最不稳）
    hybrid → 规则路由 + LLM 兜底（G5，默认：高频走规则，长尾交给 LLM）
    plan   → Plan-and-Execute（G4：先拆解成有序步骤再逐步执行）

主循环的硬约束（蓝图 §6.4 工程化护栏，全部在这里落地）：
1. **最大步数 5**：防止工具反复失败时无限绕圈烧 token；
2. **单步超时**：executor 用 asyncio.wait_for 兜底（默认 3s）；
3. **无工具证据不下事实性结论**：guardrail.check_output 的 G-05，
   事实性意图拿不到工具结果时，回复被替换成"我没查到"，而不是让模型编；
4. **低置信度转人工**：guardrail.check_confidence 的 G-10；
5. **写操作二次确认**：pending action 机制 + guardrail 的 G-04。

两个设计取舍值得记录：
- **LLM 实例不共享可变状态**：`_LLMAdapter` 每次请求新建，只借用底层 client。
  MockLLM 需要携带会话槽位才能做跨步指代，如果直接改共享实例，
  并发请求之间会串数据（评测并发跑时尤其明显）。
- **两段式 LLM 调用**：先用非流式 complete 拿到"要不要调工具"的决策，
  工具执行完再（可选）流式产出最终回答。这样既有流式体验，
  又不会出现"流到一半发现还要调工具"的尴尬。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.agent import prompts
from app.agent.executor import ToolExecutor
from app.agent.guardrail import Guardrail
from app.agent.intent import INTENTS, IntentClassifier, IntentResult
from app.agent.planner import Plan, Step
from app.agent.planner import Planner, is_composite
from app.config import Settings, get_settings
from app.errors import SmartLibError
from app.llm.base import BaseLLM, ChatMessage, LLMResult
from app.llm.mock import KNOWN_BOOKS, MockLLM
from app.llm.router import LLMRouter
from app.memory.reader_profile import get_profile_store
from app.memory.session_store import Session, get_session_store
from app.metrics import METRICS
from app.tools import registry
from app.tools.base import ToolContext, ToolResult


# =============================================================================
# 结果对象
# =============================================================================
@dataclass
class AgentResult:
    """一次对话的完整结果（含全部可观测字段）。"""

    reply: str
    trace_id: str
    session_id: str
    intent: str = ""
    intent_confidence: float = 0.0
    intent_source: str = "rule"
    mode: str = "hybrid"
    provider: str = "mock"
    steps: int = 0
    tool_evidence: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    rag_sources: list[str] = field(default_factory=list)
    handoff: bool = False
    latency_ms: float = 0.0
    guardrail_notes: list[str] = field(default_factory=list)
    degraded: bool = False
    plan: dict[str, Any] | None = None
    pending_confirm: dict[str, Any] | None = None
    first_token_ms: float | None = None
    #: 本次回复由护栏直接给出（拒绝/转人工）。置位后输出护栏不再改写它，
    #: 否则"越权拒绝"会被"无证据不下结论"覆盖成一句无关的兜底话术。
    guardrail_blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "reply": self.reply,
            "intent": self.intent,
            "intent_label": INTENTS.get(self.intent, {}).get("label", self.intent),
            "intent_confidence": round(self.intent_confidence, 3),
            "intent_source": self.intent_source,
            "mode": self.mode,
            "provider": self.provider,
            "steps": self.steps,
            "tool_evidence": self.tool_evidence,
            "tool_calls": self.tool_calls,
            "rag_sources": self.rag_sources,
            "handoff": self.handoff,
            "latency_ms": round(self.latency_ms, 1),
            "guardrail_notes": self.guardrail_notes,
            "degraded": self.degraded,
            "plan": self.plan,
            "pending_confirm": self.pending_confirm,
        }


# =============================================================================
# LLM 适配器
# =============================================================================
class _LLMAdapter:
    """把底层 LLM 包一层，给 MockLLM 挂上"本次请求私有"的槽位。

    只做两件事：
    1. 调用前后同步 slots（MockLLM 靠它做跨步指代）；
    2. 统一异常处理：LLM 任何失败都不能把整轮对话打断，返回 None 让上层走规则兜底。
    """

    def __init__(self, base: BaseLLM | None, slots: dict[str, Any]) -> None:
        self.base = base
        self.slots = slots
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return self.base is not None

    @property
    def is_mock(self) -> bool:
        return isinstance(self.base, MockLLM)

    async def complete(
        self, messages: list[ChatMessage], *, tools: list[dict[str, Any]] | None = None
    ) -> LLMResult | None:
        if self.base is None:
            return None
        if isinstance(self.base, MockLLM):
            self.base.slots = dict(self.slots)
            self.base.reader_id = self.slots.get("reader_id") or self.base.reader_id
        try:
            return await self.base.complete(messages, tools=tools)
        except Exception as exc:  # noqa: BLE001 - LLM 失败必须降级，不能 500
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
    async def stream(
        self, messages: list[ChatMessage], *, tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[str]:
        if self.base is None:
            return
        if isinstance(self.base, MockLLM):
            self.base.slots = dict(self.slots)
        try:
            async for piece in self.base.stream(messages, tools=tools):
                yield piece
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            return


# =============================================================================
# 运行时
# =============================================================================
class AgentRuntime:
    """Agent 运行时。一个实例可服务多个并发请求（无请求级可变状态）。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        llm: BaseLLM | None = None,
        java_client: Any = None,
        guardrail: Guardrail | None = None,
        classifier: IntentClassifier | None = None,
        planner: Planner | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm_router = llm if isinstance(llm, LLMRouter) else None
        self.llm: BaseLLM | None = llm or (self.llm_router.primary if self.llm_router else None)
        self.java_client = java_client
        self.guardrail = guardrail or Guardrail(self.settings)
        self.classifier = classifier or IntentClassifier(self.llm, self.settings)
        self.planner = planner or Planner(self.llm, self.settings)
        self.sessions = get_session_store(self.settings)
        self.profiles = get_profile_store(self.settings)

    # ---------------- 对外主入口 ----------------
    async def run(
        self,
        message: str,
        *,
        session_id: str | None = None,
        reader_id: int | None = None,
        mode: str | None = None,
        trace_id: str | None = None,
    ) -> AgentResult:
        started = time.perf_counter()
        trace = trace_id or f"tr-{uuid.uuid4().hex[:16]}"
        agent_mode = (mode or self.settings.agent_mode).lower()
        session = self.sessions.get_or_create(session_id, reader_id=reader_id)

        result = AgentResult(
            reply="",
            trace_id=trace,
            session_id=session.session_id,
            mode=agent_mode,
            provider=getattr(self.llm, "provider", "none") or "none",
        )

        # ---- 0. 输入护栏 ----
        input_decision = self.guardrail.check_input(message)
        result.guardrail_notes.extend(input_decision.notes)

        # ---- 1. 写操作确认的后续轮：优先处理 pending ----
        if input_decision.allowed and session.pending is not None:
            pending_result = await self._handle_pending(message, session, trace, agent_mode, result)
            if pending_result is not None:
                return self._finalize(result, session, started)

        if not input_decision.allowed and input_decision.action in ("refuse",):
            result.reply = input_decision.verdict.message
            result.intent = "guardrail_block"
            result.intent_confidence = 1.0
            result.handoff = input_decision.action == "handoff"
            self._remember(session, message, result.reply)
            return self._finalize(result, session, started)

        # ---- 2. 意图识别 ----
        intent = await self.classifier.classify(
            message, mode=agent_mode, history=session.history(self.settings.session_max_turns)
        )
        result.intent = intent.intent
        result.intent_confidence = intent.confidence
        result.intent_source = intent.source

        slots = self._merge_slots(session, intent, message, reader_id)

        # ---- 3. 低置信度转人工 ----
        conf_decision = self.guardrail.check_confidence(intent.confidence, intent.intent)
        if conf_decision.action == "handoff" and intent.intent in ("unknown",):
            result.guardrail_notes.extend(conf_decision.notes)
            result.reply = conf_decision.verdict.message
            result.handoff = True
            self._remember(session, message, result.reply)
            return self._finalize(result, session, started)

        # ---- 4. 分流执行 ----
        adapter = _LLMAdapter(self.llm, slots)
        executor = ToolExecutor(
            ToolContext(
                trace_id=trace,
                reader_id=slots.get("reader_id"),
                session_id=session.session_id,
                java=self.java_client,
                settings=self.settings,
                meta={"mode": agent_mode},
            )
        )

        # 走多步编排的两种情况：
        # (a) 复合诉求（"借书 + 推荐 + 查逾期"）；
        # (b) **续借但还不知道 borrowId**：renew 的入参是借阅记录 ID，
        #     读者只说了书名时必须先查 my_loans 做映射。这不是"顺手优化"，
        #     而是不做就一定失败的硬依赖（实测：不查记录时 renew 参数校验直接不过）。
        needs_loan_lookup = intent.tool == "renew" and not slots.get("borrow_id")
        if agent_mode == "plan" or (
            agent_mode in ("hybrid", "llm") and (is_composite(message) or needs_loan_lookup)
        ):
            await self._run_plan(message, slots, session, adapter, executor, result, agent_mode)
        else:
            await self._run_stepwise(message, slots, session, adapter, executor, result, agent_mode)

        result.tool_calls = [t.model_dump() for t in executor.traces]
        result.steps = max(executor.steps, len(result.tool_calls))
        result.tool_evidence = executor.has_evidence()
        result.degraded = result.degraded or (adapter.last_error is not None)

        # ---- 5. 输出护栏（无证据不下结论 / 长度 / 敏感）----
        output_decision = self.guardrail.check_output(
            result.reply,
            intent=result.intent,
            has_evidence=result.tool_evidence,
            tool_error_codes=executor.error_codes(),
            handoff=result.handoff,
            awaiting_confirm=result.pending_confirm is not None,
            guardrail_blocked=result.guardrail_blocked,
        )
        result.guardrail_notes.extend(output_decision.notes)
        if output_decision.action in ("replace", "refuse", "truncate", "handoff"):
            result.reply = output_decision.verdict.message
            result.handoff = result.handoff or output_decision.action == "handoff"

        if not result.reply:
            result.reply = prompts.FALLBACK_MESSAGES["no_evidence"]

        self._remember(session, message, result.reply)
        return self._finalize(result, session, started)

    # ---------------- 单步 ReAct ----------------
    async def _run_stepwise(
        self,
        message: str,
        slots: dict[str, Any],
        session: Session,
        adapter: _LLMAdapter,
        executor: ToolExecutor,
        result: AgentResult,
        mode: str,
    ) -> None:
        """ReAct 单步循环：决定工具 → 执行 → 渲染。

        迭代上限 max_steps（默认 5）。每轮只允许一个新工具调用，
        且同一"工具+参数"指纹重复会被 executor 的循环检测拦下。
        """
        intent = IntentResult(result.intent, result.intent_confidence, slots=slots)
        tool_name = intent.tool

        # rule / hybrid 的高置信度分支：直接按意图路由到工具，不花 LLM 调用
        if mode == "rule" or (mode == "hybrid" and result.intent_confidence >= 0.7):
            decision_source = "rule_route"
        else:
            decision_source = "llm"

        observations: list[ToolResult] = []
        executed: set[str] = set()

        for _step in range(self.settings.max_steps):
            call: tuple[str, dict[str, Any]] | None = None

            if decision_source == "rule_route" and tool_name and tool_name not in executed:
                args = self._default_args(tool_name, slots, confirmed=self._is_confirming(message, session))
                call = (tool_name, args)
            elif adapter.available:
                # LLM 决策：把已有观察结果与工具清单交出去，由它决定下一步
                llm_result = await self._ask_llm_for_action(
                    adapter, message, slots, observations, executed
                )
                if llm_result is not None and llm_result.has_tool_calls:
                    tc = llm_result.tool_calls[0]
                    if tc.name in registry.names() and tc.name not in executed:
                        merged = self._default_args(tc.name, slots, confirmed=self._is_confirming(message, session))
                        # LLM 给的参数优先，缺失的用确定性参数补齐
                        merged.update({k: v for k, v in tc.arguments.items() if v is not None})
                        call = (tc.name, merged)
                elif llm_result is not None and llm_result.content:
                    result.reply = llm_result.content
                    break

            if call is None:
                break

            name, args = call
            decision = self.guardrail.check_tool_call(
                name,
                args,
                session_reader_id=slots.get("reader_id"),
                confirmed=self._guardrail_confirmed(name, args, session),
            )
            result.guardrail_notes.extend(decision.notes)

            if decision.action in ("ask", "refuse", "handoff"):
                if decision.verdict.rule == "G-04_write_needs_confirm":
                    # 只有"缺确认"才排队 pending；越权/无关等 refuse 绝不能进 pending 队列
                    session.set_pending(name, args)
                    result.pending_confirm = {"tool": name, "args": args}
                    result.reply = decision.verdict.message
                else:
                    # 越权等直接拒绝：标记为"护栏已给出权威话术"，
                    # 后续输出护栏不能再拿"无证据"把它替换掉（否则读者看不到拒绝原因）
                    result.reply = decision.verdict.message
                    result.guardrail_blocked = True
                executed.add(name)
                break

            tool_result = await executor.execute(name, args)
            observations.append(tool_result)
            executed.add(name)
            self._absorb_slots(slots, name, tool_result)

            if not tool_result.ok and tool_result.error_code in (
                "CONFIRMATION_REQUIRED",
                "FORBIDDEN_OPERATION",
            ):
                result.reply = tool_result.error_message or ""
                break
            if mode == "rule":
                break
            # LLM 模式下还要继续问下一轮，直到模型说"不用再调工具了"
            if decision_source == "rule_route":
                break

        if not result.reply:
            result.reply = await self._render_answer(message, slots, observations, adapter, result)

        # 规则问答的出处要单独回传（前端展示"来源"）
        self._collect_rag_sources(result, observations)

    # ---------------- Plan-and-Execute ----------------
    async def _run_plan(
        self,
        message: str,
        slots: dict[str, Any],
        session: Session,
        adapter: _LLMAdapter,
        executor: ToolExecutor,
        result: AgentResult,
        mode: str,
    ) -> None:
        """多步编排：拆解 → 逐步执行 → 汇总回答。"""
        use_llm_plan = mode in ("plan", "llm") and adapter.available and not adapter.is_mock
        plan: Plan = await self.planner.plan(message, slots=slots, use_llm=use_llm_plan)
        if not plan.steps:
            # 拆不出多步就退回单步路径（例如复合信号被误判）
            await self._run_stepwise(message, slots, session, adapter, executor, result, mode)
            return

        observations: list[ToolResult] = []
        failed: list[str] = []

        for _ in range(self.settings.max_steps):
            step = plan.next_pending()
            if step is None:
                break
            name = step.tool
            if not name or not registry.exists(name):
                plan.mark(step, "skipped", "工具不在白名单")
                continue

            args = self._default_args(name, slots, confirmed=self._is_confirming(message, session))
            args.update({k: v for k, v in (step.args or {}).items() if v is not None})

            decision = self.guardrail.check_tool_call(
                name,
                args,
                session_reader_id=slots.get("reader_id"),
                confirmed=self._guardrail_confirmed(name, args, session),
            )
            result.guardrail_notes.extend(decision.notes)
            if decision.action in ("ask", "refuse", "handoff"):
                if decision.verdict.rule == "G-04_write_needs_confirm":
                    # 写操作卡在确认上：把这一步留成 pending，其余步骤先做完
                    session.set_pending(name, args)
                    result.pending_confirm = {"tool": name, "args": args}
                    plan.mark(step, "skipped", "等待用户确认")
                else:
                    plan.mark(step, "failed", decision.verdict.message)
                    failed.append(name)
                    if not step.optional:
                        result.reply = decision.verdict.message
                        result.guardrail_blocked = True
                        break
                continue

            tool_result = await executor.execute(name, args)
            observations.append(tool_result)
            if tool_result.ok:
                plan.mark(step, "done", tool_result.summary)
                self._absorb_slots(slots, name, tool_result)
            else:
                plan.mark(step, "failed", tool_result.error_message or "")
                failed.append(name)
                # 关键步骤失败就停：继续跑后续步骤只会产生更混乱的半成品结果
                if not step.optional and name in ("borrow", "renew"):
                    break

        result.plan = plan.to_dict()
        result.degraded = result.degraded or bool(failed)
        self._collect_rag_sources(result, observations)

        if not result.reply:
            if observations:
                result.reply = await self._render_answer(message, slots, observations, adapter, result)
            else:
                result.reply = prompts.FALLBACK_MESSAGES["no_evidence"]

        # 本轮有写操作卡在二次确认上：必须在回复里明确说明"等你确认"，
        # 否则读者只看到"库存可借 3 册"会以为已经借好了（这是实测踩过的坑）。
        if result.pending_confirm and "确认" not in result.reply:
            tool_name = result.pending_confirm.get("tool")
            if tool_name == "borrow":
                result.reply += (
                    "\n\n要我现在就为您办理这本书的借阅吗？"
                    "这是会产生真实借阅记录的操作，请回复“确认借阅”我再提交。"
                )
            elif tool_name == "renew":
                result.reply += "\n\n要我现在就提交续借吗？请回复“确认续借”我再提交。"

    # ---------------- LLM 决策辅助 ----------------
    async def _ask_llm_for_action(
        self,
        adapter: _LLMAdapter,
        message: str,
        slots: dict[str, Any],
        observations: list[ToolResult],
        executed: set[str],
    ) -> LLMResult | None:
        messages: list[ChatMessage] = [
            ChatMessage(
                role="system",
                content=prompts.REACT_SYSTEM_PROMPT.format(
                    context=json.dumps(
                        {**slots, "executed": sorted(executed)}, ensure_ascii=False, default=str
                    )
                ),
            )
        ]
        for obs in observations:
            messages.append(
                ChatMessage(
                    role="tool",
                    name=obs.tool,
                    content=json.dumps(obs.to_observation(), ensure_ascii=False, default=str),
                    tool_call_id=f"call_{obs.tool}",
                )
            )
        messages.append(ChatMessage(role="user", content=message))
        # 已完成过的工具不再提供给模型，这是"防止 LLM 反复调同一个工具"的第一道闸
        available = [t.llm_spec() for t in registry.all_tools() if t.name not in executed]
        return await adapter.complete(messages, tools=available)

    async def _render_answer(
        self,
        message: str,
        slots: dict[str, Any],
        observations: list[ToolResult],
        adapter: _LLMAdapter,
        result: AgentResult,
    ) -> str:
        """把工具观察结果渲染成自然语言回答。

        三条渲染路径，按"可信度优先"选择：
        1. **规则问答** → 确定性渲染。知识库里的数字（0.2 元/天、3 天）绝不能经模型改写；
        2. **真实 LLM** → 让它基于观察数据组织语言。注意这里**不传 tools**：
           这一步的职责是"表达"而不是"决策"，传了工具它会又想去调工具，
           导致答案变成一句"我再查一下"（实测过，是很容易踩的坑）；
        3. **Mock / LLM 降级** → 用 MockLLM 的确定性渲染器。这样离线演示与
           --provider mock 的评测结果完全可复现。
        """
        if not observations:
            return prompts.FALLBACK_MESSAGES["no_evidence"]

        if all(o.tool == "policy_qa" for o in observations):
            rendered = self._render_policy(observations[-1])
            if rendered:
                return rendered

        if adapter.available and not adapter.is_mock:
            payload = json.dumps(
                [o.to_observation() for o in observations], ensure_ascii=False, default=str
            )
            llm_result = await adapter.complete(
                [
                    ChatMessage(
                        role="user",
                        content=prompts.build_final_answer_prompt(message, payload),
                    )
                ],
                tools=None,
            )
            if llm_result is not None and llm_result.content.strip():
                return llm_result.content.strip()

        # 确定性渲染：入参是 [(工具名, 观察结果 dict)]，不是 ToolResult 列表。
        # 这里必须转成 to_observation() 的格式，否则渲染器拿不到 data 会走兜底话术。
        renderer = adapter.base if isinstance(adapter.base, MockLLM) else MockLLM(self.settings)
        pairs = [(o.tool, o.to_observation()) for o in observations]
        return renderer._render(pairs, message)  # noqa: SLF001 - 同一个类的内部渲染器，刻意复用

    def _render_policy(self, observation: ToolResult) -> str:
        data = observation.data if isinstance(observation.data, dict) else {}
        answer = str(data.get("answer") or "").strip()
        chunks = data.get("chunks") or []
        if not answer and chunks:
            answer = str(chunks[0].get("text") or "")
        if not answer:
            return ""
        source = str(data.get("source") or "")
        # 去掉知识库 chunk 里的【标题路径】面包屑（chunk 文本第一行就是它），
        # 它只用于检索定位，贴给读者是噪声
        body = re.sub(r"^\s*【[^】]*】\s*", "", answer).strip()
        if source and "规则库" not in source:
            return f"{body}\n（依据：{source.split('#')[-1]}）"
        return body

    # ---------------- pending 确认处理 ----------------
    async def _handle_pending(
        self,
        message: str,
        session: Session,
        trace: str,
        mode: str,
        result: AgentResult,
    ) -> AgentResult | None:
        """处理"上一轮询问确认、这一轮用户给答复"的场景。

        返回 None 表示不是确认场景（交给正常流程处理）。
        """
        from app.llm.mock import is_confirmation

        pending = session.pending
        assert pending is not None
        if pending.expired:
            session.clear_pending()
            return None

        if not is_confirmation(message):
            # 用户没说确认（可能改主意问了别的）→ 丢弃 pending，重新走正常流程
            session.clear_pending()
            return None

        session.pop_pending()
        args = dict(pending.args)
        args["confirm"] = True
        executor = ToolExecutor(
            ToolContext(
                trace_id=trace,
                reader_id=session.reader_id,
                session_id=session.session_id,
                java=self.java_client,
                settings=self.settings,
                meta={"mode": mode, "confirmed": True},
            )
        )
        result.intent = self._intent_of_tool(pending.tool) or result.intent
        result.intent_confidence = 1.0
        result.intent_source = "pending_confirm"

        decision = self.guardrail.check_tool_call(
            pending.tool, args, session_reader_id=session.reader_id, confirmed=True
        )
        result.guardrail_notes.extend(decision.notes)
        if decision.action != "allow":
            result.reply = decision.verdict.message
            self._remember(session, message, result.reply)
            return result

        tool_result = await executor.execute(pending.tool, args)
        result.tool_calls = [t.model_dump() for t in executor.traces]
        result.steps = executor.steps
        result.tool_evidence = executor.has_evidence()
        result.reply = tool_result.summary or tool_result.error_message or ""
        if not result.reply:
            adapter = _LLMAdapter(self.llm, {"reader_id": session.reader_id})
            result.reply = await self._render_answer(message, {}, [tool_result], adapter, result)
        # 这条路径直接返回，不经过 run() 的输出护栏：走护栏反而会被
        # "事实性意图必须有证据"误伤 —— 因为这里的 executor 只执行了 pending 这一件工具，
        # 而 evidence 判定依赖 executor.traces，逻辑上是对的；但若工具失败，
        # 我们更希望原样把失败原因（如"有人在预约排队，不能续借"）讲给读者。
        self._remember(session, message, result.reply)
        return result

    # ---------------- 参数与槽位 ----------------
    def _merge_slots(
        self,
        session: Session,
        intent: IntentResult,
        message: str,
        reader_id: int | None,
    ) -> dict[str, Any]:
        """把"请求参数 + 会话上下文 + 本轮抽取的槽位"合并成执行用槽位。

        **安全关键点**：`reader_id` 只能来自请求参数或会话，**绝不能被文本里提到的
        编号覆盖**。曾经写成 `slots.update(intent.slots)`，导致读者只要在话里写
        "帮我借读者 2002 的书"，就能把自己伪装成 2002——护栏的越权检查随即失效
        （这是实测发现的真实缺陷，不是理论风险）。
        正确做法：会话身份是权威值；文本里出现的编号放到 `claimed_reader_id`，
        由护栏拿它与会话身份比对，不一致就拒绝。
        """
        slots: dict[str, Any] = {}
        if reader_id is not None:
            slots["reader_id"] = reader_id
        elif session.reader_id is not None:
            slots["reader_id"] = session.reader_id

        extracted = {k: v for k, v in intent.slots.items() if v not in (None, "", [])}
        claimed = extracted.pop("reader_id", None)
        if claimed is not None:
            slots["claimed_reader_id"] = claimed
        slots.update(extracted)
        slots["_message"] = message
        return slots

    def _default_args(
        self, tool_name: str, slots: dict[str, Any], *, confirmed: bool
    ) -> dict[str, Any]:
        """为工具生成确定性参数。

        为什么需要这一层：LLM（尤其 Mock）不一定会把 readerId 填对，
        而 readerId 属于"系统已知的会话事实"，就不该让模型猜。
        这里先用会话事实兜住必需字段，LLM 只需补充它真正知道的业务参数。
        """
        reader_id = slots.get("reader_id")
        # 读者在话里明确点名了别的编号时，把**claimed**传给工具/护栏，
        # 这样越权检查才有东西可比对（用会话身份去填，检查会永远通过——等于没检查）。
        claimed_reader = slots.get("claimed_reader_id")
        effective_reader = claimed_reader if claimed_reader is not None else reader_id
        title = slots.get("book_title") or ""
        book_id = slots.get("book_id") or KNOWN_BOOKS.get(title)
        args: dict[str, Any] = {}
        message = slots.get("_message") or ""

        if tool_name == "search_books":
            args = {"query": title or (message.strip()[:40] or "图书"), "page": 1, "size": 5, "sortBy": "relevance"}
        elif tool_name == "get_stock":
            args = {"book_id": book_id} if book_id else {}
        elif tool_name == "borrow":
            args = {
                "readerId": effective_reader,
                "bookId": book_id,
                # 幂等键必须**稳定**：同一会话 + 同一读者 + 同一本书 → 同一个 key。
                # 后端靠它保证"网络重试/用户重复点确认"不会借两次。
                "idempotencyKey": f"ag-borrow-{effective_reader}-{book_id}",
                "confirm": confirmed,
            }
        elif tool_name == "renew":
            borrow_id = slots.get("borrow_id") or self._pick_borrow_id(slots, title)
            args = {
                "readerId": effective_reader,
                "borrowId": borrow_id,
                "idempotencyKey": f"ag-renew-{effective_reader}-{borrow_id}",
                "confirm": confirmed,
            }
        elif tool_name == "my_loans":
            args = {"readerId": effective_reader, "only_overdue": bool(slots.get("only_overdue"))}
        elif tool_name == "get_reader_profile":
            args = {"readerId": effective_reader}
        elif tool_name == "policy_qa":
            args = {"topic": (f"{title} {message}".strip() if title else message.strip()), "top_k": 3}
        elif tool_name == "recommend":
            args = {"limit": 3}
            if book_id:
                args["book_id"] = book_id
        return {k: v for k, v in args.items() if v is not None}

    @staticmethod
    def _pick_borrow_id(slots: dict[str, Any], title: str) -> int | None:
        """从已查到的借阅记录里挑出要续借的那一条。"""
        loans = slots.get("loans") or []
        if not isinstance(loans, list):
            return None
        if title:
            for item in loans:
                if isinstance(item, dict) and title in str(item.get("title") or ""):
                    return item.get("borrowId")
        if len(loans) == 1 and isinstance(loans[0], dict):
            return loans[0].get("borrowId")
        return None

    @staticmethod
    def _is_confirming(message: str, session: Session) -> bool:
        from app.llm.mock import is_confirmation

        return is_confirmation(message)

    @staticmethod
    def _guardrail_confirmed(tool_name: str, args: dict[str, Any], session: Session) -> bool:
        """护栏判定"是否已确认"的口径：参数里的 confirm 字段。"""
        return bool(args.get("confirm"))

    def _absorb_slots(self, slots: dict[str, Any], tool_name: str, result: ToolResult) -> None:
        """把工具返回的实体累积进槽位，供后续步骤复用（跨步指代的关键）。"""
        if not result.ok or not isinstance(result.data, dict):
            if tool_name == "my_loans" and result.ok and isinstance(result.data, dict):
                slots["loans"] = result.data.get("items")
            return
        data = result.data
        if tool_name == "search_books":
            items = data.get("items") or []
            slots["search_items"] = items
            title = slots.get("book_title")
            if title:
                for item in items:
                    if isinstance(item, dict) and title in str(item.get("title") or ""):
                        slots["book_id"] = item.get("bookId")
                        break
            elif items and isinstance(items[0], dict):
                slots["book_id"] = items[0].get("bookId")
                slots["book_title"] = items[0].get("title")
        elif tool_name == "get_stock":
            slots["book_id"] = data.get("bookId") or slots.get("book_id")
            slots["book_title"] = data.get("title") or slots.get("book_title")
        elif tool_name == "my_loans":
            slots["loans"] = data.get("items") or []
        elif tool_name == "borrow":
            slots["borrow_id"] = data.get("borrowId")
            slots.pop("pending_tool", None)
        elif tool_name == "renew":
            slots["new_due_date"] = data.get("newDueDate")

    @staticmethod
    def _intent_of_tool(tool_name: str) -> str | None:
        for key, meta in INTENTS.items():
            if meta["tool"] == tool_name:
                return key
        return None

    @staticmethod
    def _collect_rag_sources(result: AgentResult, observations: list[ToolResult]) -> None:
        for obs in observations:
            if obs.tool != "policy_qa" or not isinstance(obs.data, dict):
                continue
            for chunk in obs.data.get("chunks") or []:
                if isinstance(chunk, dict) and chunk.get("source"):
                    source = str(chunk["source"])
                    if source not in result.rag_sources:
                        result.rag_sources.append(source)

    # ---------------- 记忆与收尾 ----------------
    def _remember(self, session: Session, message: str, reply: str) -> None:
        session.add_user(message)
        session.add_assistant(reply)
        self.sessions.save(session)

    def _finalize(self, result: AgentResult, session: Session, started: float) -> AgentResult:
        result.latency_ms = (time.perf_counter() - started) * 1000
        METRICS.observe_chat(
            latency_ms=result.latency_ms,
            steps=result.steps,
            evidence=result.tool_evidence,
            first_token_ms=result.first_token_ms,
            labels={"mode": result.mode, "intent": result.intent},
        )
        METRICS.incr(f"intent_{result.intent}")
        return result

    # ---------------- 流式 ----------------
    async def run_stream(
        self, message: str, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        """SSE 事件流。

        事件协议（前端按 type 分支渲染）：
            {"type":"start","trace_id":...,"session_id":...,"mode":...}
            {"type":"intent","intent":...,"confidence":...}
            {"type":"tool_call","tool":...,"step":...,"args":{...}}
            {"type":"tool_result","tool":...,"ok":...,"summary":...}
            {"type":"token","text":"..."}          ← 增量文本
            {"type":"done","result":{...}}         ← 完整结果（含 trace_id 与指标）
            {"type":"error","message":"..."}
        """
        started = time.perf_counter()
        result = await self.run(message, **kwargs)
        yield {
            "type": "start",
            "trace_id": result.trace_id,
            "session_id": result.session_id,
            "mode": result.mode,
        }
        yield {
            "type": "intent",
            "intent": result.intent,
            "label": INTENTS.get(result.intent, {}).get("label", result.intent),
            "confidence": round(result.intent_confidence, 3),
            "source": result.intent_source,
        }
        for call in result.tool_calls:
            yield {
                "type": "tool_call",
                "tool": call.get("tool"),
                "step": call.get("step"),
                "args": call.get("arguments"),
            }
            yield {
                "type": "tool_result",
                "tool": call.get("tool"),
                "ok": call.get("ok"),
                "summary": call.get("summary"),
                "latency_ms": call.get("latency_ms"),
                "blocked_by": call.get("blocked_by"),
            }

        # 分片输出：真实实现里这一段应该来自 LLM 的流式接口；
        # 这里统一在结果文本上切片，保证 SSE 协议与前端渲染逻辑可以被完整测试。
        first_token: float | None = None
        for i in range(0, len(result.reply), 12):
            if first_token is None:
                first_token = (time.perf_counter() - started) * 1000
            yield {"type": "token", "text": result.reply[i : i + 12]}
        if first_token is None:
            first_token = result.latency_ms
        result.first_token_ms = first_token

        yield {"type": "done", "result": result.to_dict()}


__all__ = ["AgentRuntime", "AgentResult"]
