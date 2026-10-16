"""多步任务拆解（蓝图 §6.4 G4：Plan-and-Execute）。

蓝图给的复合任务样例：
    "帮我借《算法导论》，再推荐两本同类，顺便查我有没有逾期"

拆解必须处理**业务依赖**，这是它比"把句子按逗号切开"复杂的地方：
- renew 需要 borrowId，而读者只给了书名 → 前面必须插一步 my_loans 去映射；
- borrow 之前最好先确认有可借复本 → 插一步 get_stock（可选步骤，失败不阻断）；
- 推荐想要"同类"→ 需要书的分类，如果前面已经查过书，就把 bookId 传下去。

规则优先 + LLM 兜底同样适用于 planner：
高频复合任务（借+推荐+查逾期）用规则拆，长尾复合诉求才交给 LLM。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.prompts import PLAN_PROMPT
from app.config import Settings, get_settings
from app.llm.base import BaseLLM, ChatMessage
from app.llm.mock import KNOWN_BOOKS, extract_book_title, mock_plan
from app.tools import registry


@dataclass
class Step:
    """计划中的一步。"""

    index: int
    tool: str
    description: str
    args: dict[str, Any] = field(default_factory=dict)
    #: 可选步骤失败不阻断整体任务（例如"顺便查一下库存"）
    optional: bool = False
    status: str = "pending"  # pending | done | failed | skipped
    result_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "description": self.description,
            "args": self.args,
            "optional": self.optional,
            "status": self.status,
            "result_summary": self.result_summary,
        }


@dataclass
class Plan:
    """一次多步任务的执行计划。"""

    goal: str
    steps: list[Step] = field(default_factory=list)
    source: str = "rule"

    @property
    def tools(self) -> list[str]:
        return [s.tool for s in self.steps if s.tool]

    def next_pending(self) -> Step | None:
        for step in self.steps:
            if step.status == "pending":
                return step
        return None

    def mark(self, step: Step, status: str, summary: str = "") -> None:
        step.status = status
        step.result_summary = summary

    def done_tools(self) -> list[str]:
        return [s.tool for s in self.steps if s.status == "done"]

    def failed_tools(self) -> list[str]:
        return [s.tool for s in self.steps if s.status == "failed"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "source": self.source,
            "steps": [s.to_dict() for s in self.steps],
        }


#: 借书信号。**负向断言写在 Python 层而不是正则里**：
#: `(?!.*续借)` 放在模式开头时，re.search 会从每个位置重新尝试，
#: 从"借一下"那个位置看过去，"续借"已经被越过了 —— 断言就失效了（实测过）。
_BORROW_SIGNAL = re.compile(r"(帮我借|我要借|替我借|借一下|借这本|借那本|把.{0,8}借给我)")

#: 复合任务的特征词 → (工具, 说明)。顺序即执行顺序。
_COMPOSITE_SIGNALS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (_BORROW_SIGNAL, "borrow", "办理借阅"),
    (re.compile(r"(续借|延期)"), "renew", "办理续借"),
    (re.compile(r"(推荐|类似|相似|同类|好书)"), "recommend", "推荐同类图书"),
    (re.compile(r"(逾期|超期|还没还|未还)"), "my_loans", "查询逾期情况"),
    (re.compile(r"(我借了|我的借阅|在借|借阅)"), "my_loans", "查询在借记录"),
    (re.compile(r"(信用分|我的额度|能借几本|我的权限)"), "get_reader_profile", "查询借阅额度"),
)


def detect_composite(message: str) -> list[str]:
    """检测一条消息里包含几件事，返回按出现顺序去重后的工具序列。"""
    text = message or ""
    tools: list[str] = []
    for pattern, tool, _desc in _COMPOSITE_SIGNALS:
        if tool == "borrow" and "续借" in text:
            # "把…续借一下" / "续借一下" 里的"借"不是借新书
            continue
        if pattern.search(text) and tool not in tools:
            tools.append(tool)
    return tools


def is_composite(message: str) -> bool:
    """是否属于需要多步编排的复合任务。

    判据是"**至少两件不同的事**"，不是"有任意一件事"。
    曾经写成 `bool(detect_composite(...))`，结果单句"帮我借《X》"也走 plan 分支，
    而 plan 分支会先补一次 get_stock —— 于是同一句借书请求在两条代码路径上
    行为不一致，并且把"书查不到"时该有的诚实答复覆盖成了无意义的确认回执。
    单步写操作的库存前置核验统一交给 `AgentRuntime._ensure_book_evidence`。
    """
    return len(detect_composite(message)) >= 2


class Planner:
    """把复合诉求拆成有序步骤。"""

    def __init__(self, llm: BaseLLM | None = None, settings: Settings | None = None) -> None:
        self.llm = llm
        self.settings = settings or get_settings()

    async def plan(
        self,
        message: str,
        *,
        slots: dict[str, Any] | None = None,
        use_llm: bool = False,
    ) -> Plan:
        slots = dict(slots or {})
        rule_plan = self._plan_by_rules(message, slots)
        if rule_plan is not None and not use_llm:
            return rule_plan
        if use_llm and self.llm is not None:
            llm_plan = await self._plan_by_llm(message, slots)
            if llm_plan is not None and llm_plan.steps:
                return llm_plan
        return rule_plan or Plan(goal=message, steps=[], source="rule")

    # ---------------- 规则拆解 ----------------
    def _plan_by_rules(self, message: str, slots: dict[str, Any]) -> Plan | None:
        tools = detect_composite(message)
        if not tools:
            return None

        title = extract_book_title(message) or slots.get("book_title") or ""
        book_id = slots.get("book_id") or KNOWN_BOOKS.get(title or "")
        steps: list[Step] = []
        index = 1

        for tool in tools:
            if tool == "my_loans":
                steps.append(
                    Step(
                        index=index,
                        tool="my_loans",
                        description="查询在借与逾期情况",
                        args={"only_overdue": False},
                    )
                )
                index += 1
            elif tool == "get_reader_profile":
                steps.append(
                    Step(index=index, tool="get_reader_profile", description="查询借阅额度与信用分")
                )
                index += 1
            elif tool == "borrow":
                # 借之前先确认有可借复本（可选步骤：查库存失败也允许继续尝试借阅，
                # 因为最终裁决权在后端的 RuleEngine，Agent 侧不重复实现业务规则）
                if book_id is not None:
                    steps.append(
                        Step(
                            index=index,
                            tool="get_stock",
                            description="确认该书是否有可借复本",
                            args={"book_id": book_id},
                            optional=True,
                        )
                    )
                    index += 1
                steps.append(
                    Step(
                        index=index,
                        tool="borrow",
                        description=f"办理借阅{'《' + title + '》' if title else ''}",
                        args={"bookId": book_id} if book_id else {},
                    )
                )
                index += 1
            elif tool == "renew":
                # 续借依赖 borrowId：读者只给书名时，先查借阅记录把它映射出来
                if not slots.get("borrow_id"):
                    steps.append(
                        Step(
                            index=index,
                            tool="my_loans",
                            description="找到要续借的借阅记录号",
                            args={"only_overdue": False},
                        )
                    )
                    index += 1
                steps.append(
                    Step(index=index, tool="renew", description="办理续借", args={})
                )
                index += 1
            elif tool == "recommend":
                steps.append(
                    Step(
                        index=index,
                        tool="recommend",
                        description="推荐同类图书",
                        args={"book_id": book_id} if book_id else {},
                    )
                )
                index += 1

        if not steps:
            return None
        # 去掉重复的工具步骤：我的借阅记录一次调用就能拿到全部记录，
        # 排两步只会让工具被调两次、答案里同一份数据出现两遍（实测踩过）。
        # 保留第一次出现的位置，其余按原顺序过滤。
        deduped: list[Step] = []
        seen: set[str] = set()
        for step in steps:
            if step.tool in seen:
                continue
            seen.add(step.tool)
            step.index = len(deduped) + 1
            deduped.append(step)
        return Plan(goal=message.strip()[:60], steps=deduped, source="rule")

    # ---------------- LLM 拆解 ----------------
    async def _plan_by_llm(self, message: str, slots: dict[str, Any]) -> Plan | None:
        if self.llm is None:
            return None
        tool_lines = "\n".join(f"- {t.name}：{t.description}" for t in registry.all_tools())
        prompt = PLAN_PROMPT.format(
            question=message, slots=json.dumps(slots, ensure_ascii=False), tools=tool_lines
        )
        try:
            result = await self.llm.complete([ChatMessage(role="user", content=prompt)])
        except Exception:  # noqa: BLE001 - LLM 拆解失败必须退回规则
            return None

        payload = _parse_plan_json(result.content)
        if not payload:
            return None
        raw_steps = payload.get("steps") or []
        steps: list[Step] = []
        for i, item in enumerate(raw_steps[:4], start=1):
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "")
            if not registry.exists(tool):
                # LLM 幻觉出的工具直接丢弃（而不是报错），护栏的白名单在这里再兜一次
                continue
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            steps.append(
                Step(
                    index=i,
                    tool=tool,
                    description=str(item.get("description") or "")[:60],
                    args=args,
                )
            )
        if not steps:
            return None
        return Plan(goal=str(payload.get("goal") or message)[:60], steps=steps, source="llm")

    def plan_from_mock_helper(self, message: str) -> Plan:
        """用 MockLLM 里的确定性拆解构造计划（便于离线对比演示）。"""
        raw = mock_plan(message)
        steps = [
            Step(
                index=int(item.get("step") or i),
                tool=str(item.get("tool") or ""),
                description=str(item.get("description") or ""),
                args=item.get("args") if isinstance(item.get("args"), dict) else {},
            )
            for i, item in enumerate(raw, start=1)
        ]
        steps = [s for s in steps if s.tool and registry.exists(s.tool)]
        return Plan(goal=message.strip()[:60], steps=steps, source="mock")


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_plan_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    m = _JSON_BLOCK.search(raw)
    if not m:
        return None
    try:
        payload = json.loads(m.group(0))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


__all__ = ["Plan", "Step", "Planner", "detect_composite", "is_composite"]
