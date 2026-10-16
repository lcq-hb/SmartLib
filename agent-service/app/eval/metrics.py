"""评测指标计算。

指标口径要与蓝图 §7 逐一对应，并且**每个指标的"算不出来"情况都要显式处理**：
如果某个指标没有任何可评测的样本（例如数据集里没有写操作用例），
报告里必须显示 `null` 而不是 0——把"没测"显示成"0 分"是评测里最常见的数据造假方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 蓝图 §7 的 Agent 指标目标值
SLO: dict[str, float] = {
    "intent_accuracy": 0.92,
    "tool_arg_accuracy": 0.95,
    "e2e_success_rate": 0.88,
    "first_token_latency_ms": 1500.0,
    "tool_evidence_ratio": 1.0,
}


@dataclass
class CaseOutcome:
    """单条用例的执行结果与判定。"""

    case_id: str
    category: str
    message: str
    expect_intent: str = ""
    actual_intent: str = ""
    expect_tools: list[str] = field(default_factory=list)
    actual_tools: list[str] = field(default_factory=list)
    expect_args: dict[str, Any] = field(default_factory=dict)
    #: 各子判定的结果
    intent_ok: bool | None = None
    tools_ok: bool | None = None
    args_ok: bool | None = None
    e2e_ok: bool | None = None
    #: 观测值
    steps: int = 0
    latency_ms: float = 0.0
    tool_evidence: bool = False
    reply: str = ""
    error: str | None = None
    tools_expected: bool = True
    #: 该项是否**必须**有工具证据才能算合格（蓝图 §7 硬约束的分母口径）。
    #: 护栏拒绝（越权/敏感）与"知识库确实没有这条"的诚实答复不算——
    #: 前者本就不该给数据，后者本来就该说"没查到"。把这两类算进分母，
    #: 等于在用指标惩罚正确的安全行为，最终会引导实现去掩盖拒绝。
    requires_evidence: bool = True
    #: 未通过证据要求的具体原因
    evidence_gap: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "message": self.message,
            "expect_intent": self.expect_intent,
            "actual_intent": self.actual_intent,
            "expect_tools": self.expect_tools,
            "actual_tools": self.actual_tools,
            "intent_ok": self.intent_ok,
            "tools_ok": self.tools_ok,
            "args_ok": self.args_ok,
            "e2e_ok": self.e2e_ok,
            "steps": self.steps,
            "latency_ms": round(self.latency_ms, 1),
            "tool_evidence": self.tool_evidence,
            "requires_evidence": self.requires_evidence,
            "evidence_gap": self.evidence_gap,
            "reply": self.reply[:200],
            "error": self.error,
        }


def _ratio(numerator: int, denominator: int) -> float | None:
    """分母为 0 时返回 None（表示"该指标本次不可评"），而不是 0。"""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _pct_text(value: float | None) -> str:
    return "—" if value is None else f"{value:.2%}"


def compute_metrics(outcomes: list[CaseOutcome]) -> dict[str, Any]:
    """按蓝图 §7 的口径汇总指标。"""
    total = len(outcomes)

    intent_judged = [o for o in outcomes if o.intent_ok is not None]
    tool_judged = [o for o in outcomes if o.tools_ok is not None]
    args_judged = [o for o in outcomes if o.args_ok is not None]
    e2e_judged = [o for o in outcomes if o.e2e_ok is not None]

    latencies = [o.latency_ms for o in outcomes if o.latency_ms > 0]
    steps = [o.steps for o in outcomes if o.e2e_ok is not None]

    evidence_required = [o for o in outcomes if o.requires_evidence]
    evidence_ok = [o for o in evidence_required if o.tool_evidence]
    evidence_missing = [o for o in evidence_required if not o.tool_evidence]
    #: 原始比例（把"该有证据的"全部算进分母），用于和蓝图 §7 的 100% 硬约束对齐展示
    raw_required = [o for o in outcomes if o.tools_expected]
    raw_ok = [o for o in raw_required if o.tool_evidence]
    excluded = [
        {"case_id": o.case_id, "category": o.category, "reason": o.evidence_gap or "不适用"}
        for o in outcomes
        if o.tools_expected and not o.requires_evidence
    ]

    metrics = {
        "total_cases": total,
        "intent_accuracy": _ratio(sum(1 for o in intent_judged if o.intent_ok), len(intent_judged)),
        "tool_selection_accuracy": _ratio(
            sum(1 for o in tool_judged if o.tools_ok), len(tool_judged)
        ),
        "tool_arg_accuracy": _ratio(sum(1 for o in args_judged if o.args_ok), len(args_judged)),
        "e2e_success_rate": _ratio(sum(1 for o in e2e_judged if o.e2e_ok), len(e2e_judged)),
        "tool_evidence_ratio": _ratio(len(evidence_ok), len(evidence_required)),
        "avg_steps": round(sum(steps) / len(steps), 2) if steps else None,
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "p50_latency_ms": _percentile(latencies, 0.5),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "samples": {
            "intent": len(intent_judged),
            "tool_selection": len(tool_judged),
            "tool_args": len(args_judged),
            "e2e": len(e2e_judged),
            "evidence_required": len(evidence_required),
            "evidence_raw": len(raw_required),
        },
        "slo": _slo_check(
            {
                "intent_accuracy": _ratio(
                    sum(1 for o in intent_judged if o.intent_ok), len(intent_judged)
                ),
                "tool_arg_accuracy": _ratio(
                    sum(1 for o in args_judged if o.args_ok), len(args_judged)
                ),
                "e2e_success_rate": _ratio(
                    sum(1 for o in e2e_judged if o.e2e_ok), len(e2e_judged)
                ),
                "tool_evidence_ratio": _ratio(len(evidence_ok), len(evidence_required)),
            }
        ),
    }
    return metrics


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    idx = (len(ordered) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return round(ordered[lo] * (1 - frac) + ordered[hi] * frac, 1)


def _slo_check(values: dict[str, float | None]) -> dict[str, Any]:
    """逐项给出"是否达标"。未评测的指标标为 None（unknown），不伪装成不达标。"""
    out: dict[str, Any] = {}
    for name, target in SLO.items():
        if name == "first_token_latency_ms":
            # 本脚本走同步接口，不测首 Token（流式首 Token 由 tests/test_api.py 覆盖）
            out[name] = {"target": target, "actual": None, "pass": None, "note": "本脚本不评测"}
            continue
        actual = values.get(name)
        if actual is None:
            out[name] = {"target": target, "actual": None, "pass": None, "note": "无样本"}
        else:
            out[name] = {"target": target, "actual": actual, "pass": actual >= target}
    return out


def category_breakdown(outcomes: list[CaseOutcome]) -> dict[str, dict[str, Any]]:
    """按用例类别给出成功率，便于定位"是哪一类能力拖了后腿"。"""
    buckets: dict[str, list[CaseOutcome]] = {}
    for outcome in outcomes:
        buckets.setdefault(outcome.category, []).append(outcome)

    result: dict[str, dict[str, Any]] = {}
    for category, items in sorted(buckets.items()):
        judged = [o for o in items if o.e2e_ok is not None]
        result[category] = {
            "cases": len(items),
            "e2e_pass": sum(1 for o in judged if o.e2e_ok),
            "e2e_judged": len(judged),
            "e2e_success_rate": _ratio(sum(1 for o in judged if o.e2e_ok), len(judged)),
            "intent_accuracy": _ratio(
                sum(1 for o in items if o.intent_ok), sum(1 for o in items if o.intent_ok is not None)
            ),
        }
    return result


def to_markdown(metrics: dict[str, Any], breakdown: dict[str, Any], failures: list[CaseOutcome]) -> str:
    """生成 Markdown 报告（直接贴进 docs/06 测试报告）。"""
    lines: list[str] = []
    lines.append("# SmartLib Agent 评测报告")
    lines.append("")
    lines.append(f"- 用例总数：**{metrics['total_cases']}**")
    lines.append(f"- 平均步数：**{metrics['avg_steps']}**")
    lines.append(f"- 平均延迟：**{metrics['avg_latency_ms']} ms**（P50 {metrics['p50_latency_ms']} / P95 {metrics['p95_latency_ms']}）")
    lines.append("")
    lines.append("## 核心指标")
    lines.append("")
    lines.append("| 指标 | 实测值 | 目标 | 是否达标 | 样本数 |")
    lines.append("|---|---|---|---|---|")
    labels = {
        "intent_accuracy": "意图识别准确率",
        "tool_selection_accuracy": "工具选择准确率",
        "tool_arg_accuracy": "工具参数正确率",
        "e2e_success_rate": "端到端任务成功率",
        "tool_evidence_ratio": "事实性回答有工具证据比例",
    }
    samples = metrics.get("samples", {})
    sample_key = {
        "intent_accuracy": "intent",
        "tool_selection_accuracy": "tool_selection",
        "tool_arg_accuracy": "tool_args",
        "e2e_success_rate": "e2e",
        "tool_evidence_ratio": "evidence_required",
    }
    for key, label in labels.items():
        value = metrics.get(key)
        target = SLO.get(key)
        slo_entry = metrics["slo"].get(key, {})
        passed = slo_entry.get("pass")
        mark = "—" if passed is None else ("✅" if passed else "❌")
        show = "—" if value is None else (f"{value:.2%}" if value <= 1 else f"{value}")
        target_text = "—" if target is None else f"{target:.0%}"
        lines.append(
            f"| {label} | {show} | {target_text} | {mark} | {samples.get(sample_key[key], 0)} |"
        )
    lines.append("")
    lines.append("| 效率指标 | 实测值 |")
    lines.append("|---|---|")
    lines.append(f"| 平均步数（工具调用次数） | {metrics['avg_steps']} |")
    lines.append(f"| 平均延迟 | {metrics['avg_latency_ms']} ms |")
    lines.append(f"| P50 延迟 | {metrics['p50_latency_ms']} ms |")
    lines.append(f"| P95 延迟 | {metrics['p95_latency_ms']} ms |")
    lines.append("")

    # 证据口径必须写清楚，否则"100% 有证据"很容易变成一个自我安慰的数字
    raw = metrics.get("tool_evidence_ratio_raw")
    strict = metrics.get("tool_evidence_ratio")
    lines.append("### 工具证据口径说明")
    lines.append("")
    lines.append(f"- 严格口径（分母含全部业务用例）：**{_pct_text(raw)}**")
    lines.append(
        f"- 有效口径（分母排除\"护栏正确拒绝\"与\"知识库确实没有、已如实告知\"）：**{_pct_text(strict)}**"
    )
    excluded = metrics.get("evidence_excluded") or []
    if excluded:
        lines.append(f"- 已排除 {len(excluded)} 条（不计入证据分母）：")
        for item in excluded:
            lines.append(f"  - `{item['case_id']}`（{item['category']}）：{item['reason']}")
    missing = metrics.get("evidence_missing") or []
    if missing:
        lines.append(f"- **仍然缺证据** {len(missing)} 条（这是真实的覆盖率缺口）：")
        for item in missing:
            lines.append(f"  - `{item['case_id']}` {item['message']}：{item['reason']}")
    lines.append("")
    lines.append("## 分类别表现")
    lines.append("")
    lines.append("| 类别 | 用例数 | 端到端通过 | 端到端成功率 | 意图准确率 |")
    lines.append("|---|---|---|---|---|")
    for category, stats in breakdown.items():
        rate = stats["e2e_success_rate"]
        rate_text = "—" if rate is None else f"{rate:.0%}"
        intent_rate = stats["intent_accuracy"]
        intent_text = "—" if intent_rate is None else f"{intent_rate:.0%}"
        lines.append(
            f"| {category} | {stats['cases']} | {stats['e2e_pass']}/{stats['e2e_judged']} | {rate_text} | {intent_text} |"
        )
    lines.append("")
    if failures:
        lines.append(f"## 未通过用例（{len(failures)} 条）")
        lines.append("")
        lines.append("| 用例 | 类别 | 输入 | 期望意图 | 实际意图 | 期望工具 | 实际工具 | 实际回复（截断） |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for item in failures[:40]:
            reply = item.reply.replace("|", "/").replace("\n", " ")[:60]
            lines.append(
                f"| {item.case_id} | {item.category} | {item.message} | {item.expect_intent} | "
                f"{item.actual_intent} | {','.join(item.expect_tools) or '-'} | "
                f"{','.join(item.actual_tools) or '-'} | {reply} |"
            )
        lines.append("")
    else:
        lines.append("## 未通过用例")
        lines.append("")
        lines.append("无：全部用例通过。")
        lines.append("")
    return "\n".join(lines)


__all__ = ["CaseOutcome", "compute_metrics", "category_breakdown", "to_markdown", "SLO"]
