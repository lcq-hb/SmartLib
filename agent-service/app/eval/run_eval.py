"""Agent 评测入口（蓝图 §6.4 验收线 / §7 SLO）。

用法：
    python -m app.eval.run_eval --provider mock
    python -m app.eval.run_eval --provider mock --backend fake --mode plan
    python -m app.eval.run_eval --provider openai_compat --backend live

设计要点：
1. **可独立运行**：不依赖 pytest、不依赖真实 Java 后端（`--backend fake` 用内存后端），
   也可以在真实后端起来后切 `--backend live` 做联调验收。
2. **结果落盘**：写 `app/eval/report.json`（结构化，供 CI 断言），
   同时在 stdout 打印 Markdown 表格（供贴进 docs/06 测试报告）。
3. **不编造**：无法评测的指标显示 `—` 而不是 0；后端不可达时明确报告失败原因。
4. **每个用例之间重置会话/画像**：否则前一条用例的 pending 写操作会影响后一条，
   评测结果就不可复现了。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from app.agent.guardrail import Guardrail
from app.agent.intent import IntentClassifier
from app.agent.planner import Planner
from app.agent.runtime import AgentRuntime
from app.client.java_client import JavaClient
from app.config import Settings
from app.eval.fake_backend import EvalBackend
from app.eval.metrics import CaseOutcome, category_breakdown, compute_metrics, to_markdown
from app.llm.router import build_llm
from app.memory.reader_profile import reset_profile_store
from app.memory.session_store import get_session_store, reset_session_store
from app.rag.retriever import get_retriever

DATASET = Path(__file__).with_name("dataset.jsonl")
REPORT = Path(__file__).with_name("report.json")

#: 需要读者身份的工具，参数检查时要求 readerId 与会话一致
READER_TOOLS = {"borrow", "renew", "my_loans", "get_reader_profile"}


def load_dataset(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise SystemExit(f"数据集第 {lineno} 行不是合法 JSON：{exc}") from exc
            payload.setdefault("mode", "hybrid")
            payload.setdefault("expect_tools", [])
            payload.setdefault("expect_args", {})
            cases.append(payload)
    return cases


def check_args(
    expect: dict[str, Any],
    calls: list[dict[str, Any]],
    reply: str,
    reader_id: int | None,
    book_ids: set[int],
    pending_confirm: dict[str, Any] | None = None,
) -> tuple[bool | None, str]:
    """按数据集里约定的几种断言方式检查工具参数与回复。

    返回 (是否通过, 说明)。没有任何断言项时返回 (None, "无断言")，
    在汇总里这类用例**不计入参数正确率的分母**（不把"没测"算成"通过"）。
    """
    assertions = 0
    problems: list[str] = []

    if expect.get("has_readerId"):
        assertions += 1
        bad = [
            call
            for call in calls
            if call["tool"] in READER_TOOLS and call["arguments"].get("readerId") != reader_id
        ]
        if bad:
            problems.append(f"readerId 不匹配会话身份：{[(c['tool'], c['arguments'].get('readerId')) for c in bad]}")

    if "claimed_reader" in expect:
        assertions += 1
        claimed = expect["claimed_reader"]
        sent = [
            call
            for call in calls
            if call["arguments"].get("readerId") not in (None, reader_id)
        ]
        if sent:
            problems.append(f"越权请求把 {claimed} 发给了工具：{[c['tool'] for c in sent]}")

    if expect.get("needs_confirm"):
        assertions += 1
        write_calls = [c for c in calls if c["tool"] in ("borrow", "renew")]
        executed = [c for c in write_calls if c.get("ok")]
        if executed:
            problems.append("写操作未经确认就执行了")

    if expect.get("pending_confirm"):
        assertions += 1
        if pending_confirm is None:
            problems.append("写操作没有进入待确认状态（读者看不到该确认什么）")

    if "has_query_containing" in expect:
        assertions += 1
        needle = expect["has_query_containing"]
        search_calls = [c for c in calls if c["tool"] == "search_books"]
        if not search_calls:
            problems.append("没有调用 search_books")
        elif not any(needle in str(c["arguments"].get("query") or "") for c in search_calls):
            problems.append(f"检索关键词里没有「{needle}」：{[c['arguments'].get('query') for c in search_calls]}")

    if "book_id" in expect:
        assertions += 1
        want = expect["book_id"]
        if not any(c["arguments"].get("book_id") == want or c["arguments"].get("bookId") == want for c in calls):
            problems.append(f"没有针对书目 {want} 的调用")

    if expect.get("min_tools"):
        assertions += 1
        ok_calls = [c for c in calls if c.get("ok")]
        if len(ok_calls) < int(expect["min_tools"]):
            problems.append(f"成功工具调用 {len(ok_calls)} 次，少于要求的 {expect['min_tools']} 次")

    for needle in expect.get("answer_contains", []) or []:
        assertions += 1
        if needle not in reply:
            problems.append(f"回复里缺少「{needle}」")

    for needle in expect.get("must_not_contain", []) or []:
        assertions += 1
        if needle in reply:
            problems.append(f"回复里出现了不该有的「{needle}」")

    # 所有调用的书目 ID 必须在馆藏里（防止 LLM/Mock 凭空造 ID）
    for call in calls:
        for key in ("bookId", "book_id"):
            value = call["arguments"].get(key)
            if isinstance(value, int) and book_ids and value not in book_ids:
                assertions += 1
                problems.append(f"调用了不存在的书目 ID {value}")

    if assertions == 0:
        return None, "无断言"
    if problems:
        return False, "；".join(problems)
    return True, "通过"


async def run_case(
    case: dict[str, Any],
    runtime: AgentRuntime,
    settings: Settings,
    book_ids: set[int],
) -> CaseOutcome:
    outcome = CaseOutcome(
        case_id=str(case["id"]),
        category=str(case.get("category") or "unknown"),
        message=str(case["message"]),
        expect_intent=str(case.get("expect_intent") or ""),
        expect_tools=list(case.get("expect_tools") or []),
        expect_args=dict(case.get("expect_args") or {}),
    )

    reader_id = case.get("reader_id")
    mode = str(case.get("mode") or settings.agent_mode)
    started = time.perf_counter()
    try:
        result = await runtime.run(
            outcome.message, session_id=f"eval-{outcome.case_id}", reader_id=reader_id, mode=mode
        )
    except Exception as exc:  # noqa: BLE001 - 单条用例异常不能让整轮评测崩掉
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.latency_ms = (time.perf_counter() - started) * 1000
        outcome.intent_ok = False
        outcome.e2e_ok = False
        return outcome

    outcome.latency_ms = result.latency_ms or (time.perf_counter() - started) * 1000
    outcome.actual_intent = result.intent
    # "决定要调但因为需要二次确认而暂缓"的工具也算选中了 —— 观测上必须与
    # "模型压根没想到要调"区分开，否则工具选择准确率会把安全策略的正确行为算成错误。
    outcome.actual_tools = [t["tool"] for t in result.tool_calls] + [
        tool for tool in result.pending_tools if tool not in [t["tool"] for t in result.tool_calls]
    ]
    outcome.steps = result.steps
    outcome.tool_evidence = result.tool_evidence
    outcome.reply = result.reply

    # ---- 意图 ----
    outcome.intent_ok = result.intent == outcome.expect_intent or (
        outcome.expect_intent == "unknown" and result.intent == "guardrail_block"
    )

    # ---- 工具选择 ----
    if outcome.expect_tools:
        outcome.tools_ok = all(tool in outcome.actual_tools for tool in outcome.expect_tools)
    elif outcome.expect_intent in ("chitchat", "unknown"):
        # 闲聊/域外问题**不应该**调用任何工具
        outcome.tools_ok = not outcome.actual_tools
    elif outcome.expect_args.get("claimed_reader") is not None:
        # 越权用例：护栏必须在**被越权的那类工具**真正执行前拦下。
        # 允许出现只读的、不涉及他人数据的调用（如查库存确认书目存在），
        # 但任何带 readerId 的调用都不允许成功（那意味着拿到了别人的数据）。
        sensitive = {"borrow", "renew", "my_loans", "get_reader_profile"}
        leaked = [
            t for t in result.tool_calls if t.get("ok") and t["tool"] in sensitive
        ]
        outcome.tools_ok = not leaked and not outcome.expect_tools
    else:
        outcome.tools_ok = None

    # ---- 参数正确性 ----
    outcome.args_ok, note = check_args(
        outcome.expect_args,
        result.tool_calls,
        result.reply,
        reader_id,
        book_ids,
        result.pending_confirm,
    )
    if note not in ("通过", "无断言"):
        outcome.reply = f"{outcome.reply}  [参数检查: {note}]"

    # ---- 端到端 ----
    checks: list[bool] = []
    if outcome.intent_ok is not None:
        checks.append(bool(outcome.intent_ok))
    if outcome.tools_ok is not None:
        checks.append(bool(outcome.tools_ok))
    if outcome.args_ok is not None:
        checks.append(bool(outcome.args_ok))
    # 写操作用例：必须走到"待确认"而不是直接执行
    if outcome.expect_args.get("needs_confirm"):
        checks.append(result.pending_confirm is not None or "确认" in result.reply)
    outcome.e2e_ok = all(checks) if checks else None

    # ---- 工具证据口径（蓝图 §7 硬约束"事实性回答 100% 有工具证据"）----
    # 分母只包含"本该拿数据回答"的用例，并如实标注两类正确的例外：
    #   1) 护栏拒绝（越权/敏感）：本就不该返回任何他人数据，没有证据是正确行为；
    #   2) 知识库确实没有、助手已如实说"没查到"：这正是 G-05 期望的结果。
    # 把这两类算进分母，等于用指标惩罚正确的安全行为与诚实行为。
    factual = {
        "search_book",
        "get_stock",
        "borrow_book",
        "renew_book",
        "my_loans",
        "reader_profile",
        "policy_qa",
        "recommend",
    }
    if outcome.expect_intent not in factual:
        outcome.tools_expected = False
        outcome.requires_evidence = False
    elif result.guardrail_blocked:
        outcome.requires_evidence = False
        outcome.evidence_gap = "护栏正确拒绝（越权/敏感），不应返回数据"
    elif not result.tool_evidence and result.reply.startswith("这个我需要查一下"):
        outcome.requires_evidence = False
        outcome.evidence_gap = "知识库确实没有这条，助手已如实告知未查到"
    elif not result.tool_evidence:
        outcome.requires_evidence = True
        outcome.evidence_gap = "该拿工具数据却没有任何成功调用"
    return outcome


def build_runtime(settings: Settings, backend: Any, provider: str) -> tuple[AgentRuntime, Any]:
    llm = build_llm(settings)
    java = JavaClient(settings, transport=backend.transport() if backend else None)
    runtime = AgentRuntime(
        settings=settings,
        llm=llm,
        java_client=java,
        guardrail=Guardrail(settings),
        classifier=IntentClassifier(llm, settings),
        planner=Planner(llm, settings),
    )
    return runtime, java


async def evaluate(args: argparse.Namespace) -> int:
    settings = Settings(
        _env_file=None,
        llm_provider=args.provider,
        agent_mode=args.mode or "hybrid",
        java_base_url=args.java_base_url,
        log_level="WARNING",
    )
    if args.provider == "openai_compat" and not settings.llm_api_key:
        print(
            "!! LLM_PROVIDER=openai_compat 但未配置 LLM_API_KEY。\n"
            "   请设置环境变量 LLM_API_KEY 后重试（或用 --provider mock 跑离线评测）。",
            file=sys.stderr,
        )
        return 2

    cases = load_dataset(DATASET)
    if args.limit:
        cases = cases[: args.limit]
    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]

    backend = EvalBackend() if args.backend == "fake" else None
    runtime, java = build_runtime(settings, backend, args.provider)
    # 预热知识库索引：否则第一条 RAG 用例会把建索引的时间算进它的延迟里
    await get_retriever(settings, force_rebuild=True).build()

    mode_banner = args.mode or settings.agent_mode
    print(f"SmartLib Agent 评测｜用例 {len(cases)} 条｜LLM={args.provider}｜后端={args.backend}｜模式={mode_banner}")
    print("-" * 88)

    outcomes: list[CaseOutcome] = []
    for index, case in enumerate(cases, start=1):
        # 每条用例都从干净的记忆开始，保证可复现：上一条用例的 pending 写操作
        # （"帮我借《X》"会留下待确认队列）如果串到下一条，评测结果就不可复现了。
        # 直接 clear 单例内容并重新挂回 runtime，避免"runtime 还握着旧 store"的坑。
        reset_session_store()
        reset_profile_store()
        sessions = get_session_store(settings)
        sessions.clear()
        runtime.sessions = sessions
        outcome = await run_case(case, runtime, settings, backend.book_ids() if backend else set())
        outcomes.append(outcome)
        status = "PASS" if outcome.e2e_ok else ("FAIL" if outcome.e2e_ok is False else "N/A ")
        print(
            f"[{index:>3}/{len(cases)}] {status} {outcome.case_id} "
            f"{outcome.category:<16} {outcome.message[:28]:<30} "
            f"intent={outcome.actual_intent:<16} tools={','.join(outcome.actual_tools) or '-'}"
        )

    metrics = compute_metrics(outcomes)
    breakdown = category_breakdown(outcomes)
    failures = [o for o in outcomes if o.e2e_ok is False]
    markdown = to_markdown(metrics, breakdown, failures)

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "provider": args.provider,
        "backend": args.backend,
        "mode": mode_banner,
        "java_base_url": settings.java_base_url,
        "dataset": DATASET.name,
        "metrics": metrics,
        "by_category": breakdown,
        "failures": [o.to_dict() for o in failures],
        "cases": [o.to_dict() for o in outcomes],
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("-" * 88)
    safe_print(markdown)
    print("-" * 88)
    safe_print(f"报告已写入：{REPORT}")
    await java.aclose()
    return 0 if not failures else 1


def safe_print(text: str) -> None:
    """按当前控制台编码安全输出，替换掉无法表示的字符。

    为什么需要它：这个脚本在中文 Windows 上会以 GBK 输出 stdout。
    报告里只要有任何一个 GBK 编不出来的字符（emoji、✅/❌、某些箭头），
    `print` 就会直接抛 UnicodeEncodeError，**整份报告一个字都打不出来**，
    而且退出码是非零，看起来像"评测失败"——实际只是打印挂了。
    本项目真实踩过这个坑（`print(markdown)` 崩在 U+2705 上）。

    两个措施配合使用：
      1. 报告内的装饰性符号统一改成 GBK 可编码的写法（见 metrics.py）；
      2. 这里再用 errors="replace" 兜底，保证以后新增任何字符都不会让报告崩掉。
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        print(text)
    except (UnicodeEncodeError, LookupError):
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SmartLib Agent 评测脚本")
    parser.add_argument(
        "--provider",
        choices=["mock", "openai_compat"],
        default=os.getenv("LLM_PROVIDER", "mock"),
        help="LLM 提供方：mock（离线确定性，默认）/ openai_compat（真实模型）",
    )
    parser.add_argument(
        "--backend",
        choices=["fake", "live"],
        default="fake",
        help="Java 后端：fake（进程内模拟，离线可跑，默认）/ live（真实后端，需先启动）",
    )
    parser.add_argument("--mode", choices=["rule", "llm", "hybrid", "plan"], default=None)
    parser.add_argument("--category", default=None, help="只跑某个类别的用例")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（调试用）")
    parser.add_argument(
        "--java-base-url", default=os.getenv("JAVA_BASE_URL", "http://127.0.0.1:8080")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
