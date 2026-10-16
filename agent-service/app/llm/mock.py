"""MockLLM：确定性规则模拟的"LLM"。

存在的意义（蓝图 §3.2）：没有 API Key、没有外网、Java 后端也没起的时候，
整套能力（意图识别 → 工具调用 → RAG → 护栏 → 评测）仍然必须可演示、可测试。
它不是"假数据生成器"，而是一个**规则驱动的对话策略实现**：
它的输出格式与真实 LLM 完全一致（tool_calls + 自然语言），
所以把它换成 `openai_compat` 时，上层 runtime 一行都不用改。

设计要点：
1. **确定性**：同一输入永远同一输出（评测可复现，这是能写进 SLO 报告的前提）。
2. **两轮对话模型**：第 1 轮产出 tool_calls；看到 role=tool 的观察结果后，
   第 2 轮把结构化结果渲染成中文自然语言（即"带工具证据的回答"）。
3. **理解 Pydantic schema**：从 tools 参数里读出字段名与类型，
   而不是把参数名硬编码，这样工具 schema 改了 Mock 不会静默失效。
4. **否定与询问判定**：识别"怎么借/能借几本"这类**规则咨询**，
   避免把咨询误判成写操作（这是规则路由最容易出错的地方）。
"""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator

from app.llm.base import (
    BaseLLM,
    ChatMessage,
    LLMResult,
    ToolCall,
    dump_json,
    elapsed_ms,
    estimate_tokens,
    iter_text_chunks,
    timer,
)

# 一组能识别出"书名"的常见图书馆书目，用于确定性抽取 bookId/title 参数。
# 真实 LLM 会直接从上下文取，Mock 只能用有限词表 + 引号抽取，这是它明确的局限。
KNOWN_BOOKS: dict[str, int] = {
    "算法导论": 101,
    "计算机网络": 102,
    "高等数学": 103,
    "深入理解计算机系统": 104,
    "Java 并发编程实战": 105,
    "数据结构与算法分析": 106,
    "操作系统概念": 107,
    "数据库系统概论": 108,
    "机器学习": 109,
    "人类简史": 110,
}

#: 意图 → 首选工具（与 app/agent/intent.py 的意图分类保持一致）
INTENT_TO_TOOL: dict[str, str] = {
    "search_book": "search_books",
    "get_stock": "get_stock",
    "borrow_book": "borrow",
    "renew_book": "renew",
    "my_loans": "my_loans",
    "reader_profile": "get_reader_profile",
    "policy_qa": "policy_qa",
    "recommend": "recommend",
    "chitchat": "",
}

#: 写操作 → 用户必须明确确认
WRITE_TOOLS = {"borrow", "renew"}

# 规则咨询的触发词（问"怎么办"而不是"帮我办"）
POLICY_HINTS = (
    "怎么借", "如何借", "能借几本", "可以借几本", "借几本", "借多久", "借期", "多长时间",
    "逾期", "罚金", "罚款", "怎么还", "怎么续借", "如何续借", "能不能续借", "预约",
    "在几楼", "哪个楼层", "开放时间", "几点开", "几点关", "馆藏分布", "学科服务",
    "规则", "政策", "怎么弄", "怎么办", "流程",
)

CHITCHAT_HINTS = ("你好", "您好", "hi", "hello", "谢谢", "你是谁", "你能做什么", "在吗")


def _arg_schema(tools: list[dict[str, Any]] | None) -> dict[str, dict[str, str]]:
    """从 tools 描述里提取 {工具名: {字段名: 类型}}，用于按 schema 组装参数。"""
    out: dict[str, dict[str, str]] = {}
    for spec in tools or []:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name") or (spec.get("function") or {}).get("name")
        if not name:
            continue
        params = (
            spec.get("parameters")
            or (spec.get("function") or {}).get("parameters")
            or spec.get("parameters_schema")
            or {}
        )
        props = params.get("properties", {}) if isinstance(params, dict) else {}
        fields: dict[str, str] = {}
        for fname, fdesc in props.items():
            if isinstance(fdesc, dict):
                fields[fname] = str(fdesc.get("type", "string"))
            else:
                fields[fname] = "string"
        out[name] = fields
    return out


#: 这些词**不是书名**。抽取兜底正则很容易把"查一下读者""我的借阅记录"里的
#: 名词当成书名，一旦抽错，search_books 就会拿着"读者"去检索（实测踩过）。
NOT_TITLES = frozenset(
    {
        "书", "图书", "这本", "那本", "记录", "借阅记录", "读者", "用户", "账号",
        "信用分", "额度", "册数", "信息", "情况", "状态", "库存", "复本", "位置",
        "规则", "罚金", "订单", "列表", "数据", "我的", "一下", "多少", "几本",
        "几次", "几天", "多久", "哪些", "什么", "怎么", "如何",
    }
)


def extract_book_title(text: str) -> str | None:
    """抽取书名：优先《》引号，其次已知书目词表，最后走受限的兜底正则。

    兜底正则刻意收窄（要求"的/？/！/空"结尾 + 命中非书名黑名单就丢弃）：
    宁可抽不到（那会走规则追问或让 LLM 补），也不能抽错——
    抽错会让 search_books 拿一个无意义的词去检索，返回一堆不相关书目。
    """
    m = re.search(r"《([^》]{1,60})》", text)
    if m:
        return m.group(1).strip()
    for title in KNOWN_BOOKS:
        if title in text:
            return title
    m = re.search(
        r"(?:借|查找|查|搜|预约)\s*(?:一下|一本|1本)?\s*([\u4e00-\u9fffA-Za-z0-9]{2,20}?)(?:的|这本|那本|$|[？?！!。])",
        text,
    )
    if m:
        cand = m.group(1).strip()
        if cand and cand not in NOT_TITLES:
            return cand
    return None


def extract_reader_id(text: str) -> int | None:
    """抽取读者 ID：支持"读者 1001""readerId=1001""用户编号 1001"。"""
    for pattern in (
        r"(?:读者|用户|readerId|reader_id|编号|卡号|账号)\s*[:：=]?\s*(\d{1,10})",
        r"(?<!\d)(1\d{3,9})(?!\d)",
    ):
        m = re.search(pattern, text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def extract_borrow_id(text: str) -> int | None:
    m = re.search(r"(?:借阅记录|记录|borrowId|borrow_id)\s*[:：=]?\s*(\d{1,10})", text)
    if m:
        return int(m.group(1))
    return None


#: 明确的确认词。刻意收窄：只有用户真的表达"同意执行"，才允许放行写操作。
#: 用一个共享函数是为了让 intent 层与 runtime 层对"什么算确认"只有一个定义。
CONFIRM_WORDS = ("确认", "确定", "同意", "就借", "就续", "确认借阅", "确认续借", "好的借", "yes")


def is_confirmation(text: str) -> bool:
    """判断用户是否在明确确认执行某个写操作。

    刻意收得很紧，并且**只认真正的确认词**：
    曾经把"好/行/可以/嗯"也算进来，结果"能续借几次"里的"能"被误判成确认，
    直接把一句规则咨询当成写操作确认。教训是：短词匹配必须限定在
    "整句就是这个短词"（≤3 字），不能"包含即命中"。
    """
    if not text:
        return False
    stripped = text.strip()
    if any(word in stripped for word in CONFIRM_WORDS):
        return True
    return len(stripped) <= 3 and any(w in stripped for w in ("好", "行", "嗯"))


def mock_plan(message: str) -> list[dict[str, Any]]:
    """确定性多步拆解，供 Plan-and-Execute 模式在 Mock 下演示。

    对应蓝图 §6.4 G4 的复合任务示例：
    "帮我借《算法导论》，再推荐两本同类，顺便查我有没有逾期"
    """
    steps: list[dict[str, Any]] = []
    text = message or ""
    has_borrow = any(k in text for k in ("借", "帮我借")) and "怎么借" not in text
    has_recommend = any(k in text for k in ("推荐", "同类", "类似", "相似"))
    has_overdue = any(k in text for k in ("逾期", "未还", "超期"))
    has_loans = any(k in text for k in ("我借了", "我的借阅", "在借", "借了什么"))

    if has_borrow:
        steps.append(
            {
                "step": 1,
                "tool": "borrow",
                "description": "为读者办理借书（写操作，需要二次确认）",
                "args": {"book_title": extract_book_title(text) or "", "query": extract_book_title(text) or ""},
            }
        )
    if has_recommend:
        steps.append(
            {
                "step": len(steps) + 1,
                "tool": "recommend",
                "description": "推荐同类书目",
                "args": {"book_title": extract_book_title(text) or "", "limit": 2},
            }
        )
    if has_overdue or has_loans:
        steps.append(
            {
                "step": len(steps) + 1,
                "tool": "my_loans",
                "description": "查询读者当前借阅与逾期情况",
                "args": {"only_overdue": bool(has_overdue)},
            }
        )
    if not steps:
        steps.append(
            {
                "step": 1,
                "tool": "",
                "description": "直接回答",
                "args": {},
            }
        )
    return steps


class MockLLM(BaseLLM):
    """确定性 LLM 替身。"""

    provider = "mock"

    def __init__(
        self,
        settings: Any = None,
        *,
        reader_id: int | None = None,
        default_book_id: int = 101,
        default_borrow_id: int = 5001,
        slots: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.reader_id = reader_id or 1001
        #: 会话槽位：跨步指代的来源（"上一步说的那本书"）。runtime 每轮会更新它。
        self.slots: dict[str, Any] = dict(slots or {})
        #: Mock 没有真实语义理解时的演示默认值（真实 LLM 不需要）
        self.default_book_id = default_book_id
        self.default_borrow_id = default_borrow_id

    def update_slots(self, slots: dict[str, Any]) -> None:
        """由 runtime 在拿到工具结果后调用，把观察到的实体累积进槽位。"""
        for key, value in (slots or {}).items():
            if value not in (None, "", []):
                self.slots[key] = value

    # ---------------- 内部：决策 ----------------
    @staticmethod
    def _last_user_text(messages: list[ChatMessage]) -> str:
        for msg in reversed(messages):
            if msg.role == "user":
                return msg.content or ""
        return ""

    @staticmethod
    def _observations(messages: list[ChatMessage]) -> list[tuple[str, dict[str, Any]]]:
        """取出已有的工具观察结果：(工具名, 数据)。"""
        out: list[tuple[str, dict[str, Any]]] = []
        for msg in messages:
            if msg.role == "tool" and msg.content:
                try:
                    payload = json.loads(msg.content)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    out.append((msg.name or payload.get("tool", ""), payload))
        return out

    def _decide_tool(self, text: str) -> str:
        """核心规则路由：文本 → 工具名（空串表示直接回答）。"""
        if not text:
            return ""
        low = text.lower()
        if any(h in low for h in CHITCHAT_HINTS) and len(text) <= 12:
            return ""
        # 0) 二次确认回复：用户只回一句"确认借阅/确认续借"时，必须落回原来的写操作，
        #    否则会被后面的关键词规则抢成别的意图（实测："确认借阅《X》"被抢成检索）。
        if is_confirmation(text):
            if any(k in text for k in ("续借", "延期")):
                return "renew"
            if any(k in text for k in ("借阅", "借书", "借")):
                return "borrow"
        # 1) 规则咨询优先：问"怎么借/借几本/在几楼"是 RAG 场景，不是写操作。
        #    注意"怎么续借/能续借几次"必须先被判为规则咨询，否则会被 renew 抢走。
        if any(h in text for h in POLICY_HINTS) and not any(
            k in text for k in ("帮我借", "帮我续", "我要借", "我要续", "替我借")
        ):
            if any(k in text for k in ("逾期", "罚金", "罚款")):
                # "我有没有逾期" 是查个人数据；"逾期会罚多少钱" 才是规则问题
                if any(k in text for k in ("我", "查一下我的")):
                    return "my_loans"
            return "policy_qa"
        # 2) 个人数据类
        if any(k in text for k in ("我的借阅", "我借了", "在借", "借了什么", "还没还", "应还")):
            return "my_loans"
        if any(k in text for k in ("逾期", "超期")):
            return "my_loans"
        if any(k in text for k in ("我的信用", "信用分", "读者画像", "我的偏好", "我的信息", "我的账号")):
            return "get_reader_profile"
        # 3) 写操作
        if any(k in text for k in ("续借", "延期")):
            return "renew"
        if any(k in text for k in ("帮我借", "我要借", "替我借", "借一下", "借这本", "借那本")) or (
            "借" in text and "《" in text
        ):
            return "borrow"
        # 4) 预约/库存
        if any(k in text for k in ("预约", "排队")):
            return "policy_qa" if "怎么" in text else "get_stock"
        if any(k in text for k in ("还有几本", "有没有货", "在馆", "可借", "库存", "在几楼", "位置", "索书号")):
            return "get_stock"
        # 5) 推荐
        if any(k in text for k in ("推荐", "同类", "类似", "相似", "好书", "看什么")):
            return "recommend"
        # 6) 检索
        if any(k in text for k in ("找", "检索", "搜索", "有没有这本书", "查书", "借阅")):
            return "search_books"
        if "《" in text:
            return "search_books"
        return "policy_qa"

    def _build_args(self, tool: str, text: str, schema: dict[str, str]) -> dict[str, Any]:
        """按工具 schema 组装参数（只填 schema 里声明过的字段）。

        MockLLM 没有真实的语义理解，所以参数靠三类确定性来源拼：
        1. 当前用户消息（书名《》、读者/记录编号）；
        2. 会话槽位（runtime 通过 system/plan_step 消息传入的 slots，
           解决"上一步说的是这本书"的跨步指代）；
        3. 默认值（仅用于演示，见 self.default_book_id）。
        """
        args: dict[str, Any] = {}
        slots = dict(self.slots or {})

        title = extract_book_title(text) or slots.get("book_title")
        reader_id = extract_reader_id(text) or slots.get("reader_id") or self.reader_id
        borrow_id = extract_borrow_id(text) or slots.get("borrow_id")
        if borrow_id is None and slots.get("loans"):
            # 读者说"续借《算法导论》"时只给了书名，borrowId 要从上一步 my_loans 的结果里找
            loans = slots.get("loans") or []
            if isinstance(loans, list) and loans:
                for item in loans:
                    if not isinstance(item, dict):
                        continue
                    if title and title in str(item.get("title") or ""):
                        borrow_id = item.get("borrowId")
                        break
                if borrow_id is None and len(loans) == 1:
                    borrow_id = loans[0].get("borrowId")

        def put(key: str, value: Any) -> None:
            if key in schema:
                args[key] = value

        book_id = slots.get("book_id") or KNOWN_BOOKS.get(title or "")
        if book_id is None and title and slots.get("search_items"):
            for item in slots.get("search_items") or []:
                if isinstance(item, dict) and title in str(item.get("title") or ""):
                    book_id = item.get("bookId")
                    break

        if tool == "search_books":
            put("query", title or text.strip()[:40] or "图书")
            put("page", 1)
            put("size", 5)
            put("sortBy", "relevance")
        elif tool == "get_stock":
            put("book_id", book_id or self.default_book_id)
        elif tool == "borrow":
            put("readerId", reader_id)
            put("bookId", book_id or self.default_book_id)
            # 幂等键必须稳定：同一本书+同一个读者重复走这一步时，
            # 后端靠它保证不会借两次
            put("idempotencyKey", f"mock-borrow-{reader_id}-{book_id or self.default_book_id}")
            put("confirm", is_confirmation(text))
        elif tool == "renew":
            put("readerId", reader_id)
            put("borrowId", borrow_id or self.default_borrow_id)
            put("idempotencyKey", f"mock-renew-{reader_id}-{borrow_id or self.default_borrow_id}")
            put("confirm", is_confirmation(text))
        elif tool == "my_loans":
            put("readerId", reader_id)
            put("only_overdue", bool(re.search(r"(逾期|超期|没还|未还)", text)))
        elif tool == "get_reader_profile":
            put("readerId", reader_id)
        elif tool == "policy_qa":
            put("topic", (title + " " + text) if title and title not in text else text.strip())
            put("top_k", 3)
        elif tool == "recommend":
            put("book_id", book_id)
            put("limit", 3)
        return args

    @staticmethod
    def _plan_context(messages: list[ChatMessage]) -> dict[str, Any] | None:
        """从 system 消息里读出 planner 给出的"本步该调什么"的 JSON 计划。"""
        for msg in messages:
            if msg.role == "system" and msg.name == "plan_step" and msg.content:
                try:
                    payload = json.loads(msg.content)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    return payload
        return None

    @staticmethod
    def _render_plan_progress(plan: dict[str, Any]) -> str:
        """plan 模式下所有步骤都做完（或没得做）时的收尾文案。"""
        done = plan.get("done") or []
        failed = plan.get("failed") or []
        parts: list[str] = []
        if done:
            parts.append(f"我已经按顺序完成了：{'、'.join(done)}。")
        if failed:
            parts.append(f"其中 {len(failed)} 步没有成功：{'、'.join(failed)}。")
        if not parts:
            parts.append("这件事我没有找到可以执行的步骤，麻烦您再说清楚一点。")
        return "".join(parts)

    # ---------------- 对外：complete ----------------
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        start = timer()
        text = self._last_user_text(messages)
        schema_map = _arg_schema(tools)
        observations = self._observations(messages)

        # 已经有工具观察结果 → 生成最终自然语言回答（带工具证据）
        if observations:
            content = self._render(observations, text)
            return LLMResult(
                content=content,
                finish_reason="stop",
                usage=self._usage(messages, content),
                latency_ms=elapsed_ms(start),
                provider=self.provider,
            )

        plan = self._plan_context(messages)
        tool = ""
        args: dict[str, Any] = {}
        if plan is not None:
            # plan 模式下由 planner 指定本步工具；已完成/失败的工具通过 exclude 排除，
            # 保证多步任务不会在同一轮里反复调同一个工具（那是死循环的来源）。
            exclude = set(plan.get("exclude") or [])
            planned = str(plan.get("tool") or "").strip()
            if planned and planned not in exclude:
                tool = planned
            else:
                for step in plan.get("steps") or []:
                    name = str(step.get("tool") or "")
                    if name and name not in exclude:
                        tool = name
                        break
            if tool:
                args = self._build_args(tool, text, schema_map.get(tool, {}))
                override = plan.get("args") or {}
                for key, value in override.items():
                    if key in schema_map.get(tool, {}) and value not in (None, ""):
                        args[key] = value
            # planner 明确说"本步不需要工具"（例如收尾回答）
            if not tool:
                content = self._render_plan_progress(plan)
                return LLMResult(
                    content=content,
                    finish_reason="stop",
                    usage=self._usage(messages, content),
                    latency_ms=elapsed_ms(start),
                    provider=self.provider,
                )
        else:
            tool = self._decide_tool(text)
            if tool:
                args = self._build_args(tool, text, schema_map.get(tool, {}))

        if tool and tool in schema_map:
            call = ToolCall(name=tool, arguments=args, id=f"call_{tool}_1")
            return LLMResult(
                content="",
                tool_calls=[call],
                finish_reason="tool_calls",
                usage=self._usage(messages, dump_json(args)),
                latency_ms=elapsed_ms(start),
                provider=self.provider,
            )

        # 无工具可用 → 闲聊/兜底话术
        content = self._fallback_text(text)
        return LLMResult(
            content=content,
            finish_reason="stop",
            usage=self._usage(messages, content),
            latency_ms=elapsed_ms(start),
            provider=self.provider,
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        result = await self.complete(messages, tools=tools, temperature=temperature)
        text = result.content or ""
        if result.has_tool_calls:
            # 工具调用阶段不产出自然语言，交给 runtime 去执行工具后再流式渲染
            return
        for chunk in iter_text_chunks(text):
            yield chunk

    # ---------------- 文本渲染 ----------------
    def _usage(self, messages: list[ChatMessage], completion: str) -> dict[str, int]:
        prompt = sum(estimate_tokens(m.content or "") for m in messages)
        comp = estimate_tokens(completion)
        return {"prompt": prompt, "completion": comp, "total": prompt + comp}

    @staticmethod
    def _fallback_text(text: str) -> str:
        if any(h in text for h in ("你好", "您好", "hi", "hello", "在吗")):
            return (
                "您好！我是图书馆智能助手，可以帮您：① 找书并查到馆位置 ② 借书/续借/预约 "
                "③ 查我的在借与逾期 ④ 解答借阅规则 ⑤ 按兴趣推荐图书。请问需要办哪一件？"
            )
        if any(h in text for h in ("谢谢", "多谢", "感谢")):
            return "不客气！还有其他需要随时找我。"
        if any(h in text for h in ("你是谁", "你能做什么", "能干什么")):
            return (
                "我是 SmartLib 图书馆智能助手，接入馆藏与借阅实时数据。"
                "我可以查书、查库存与馆藏位置、办理借书续借预约、查询您的在借与逾期、解释借阅规则，并按您的偏好推荐图书。"
            )
        return (
            "抱歉，我没太理解您的意思。您可以换个说法，例如："
            "“找一下《算法导论》”“《计算机网络》还有可借的吗”“帮我续借”“我有没有逾期的书”"
            "“本科生能借几本”。"
        )

    def _render(self, observations: list[tuple[str, dict[str, Any]]], question: str) -> str:
        """把工具的结构化结果渲染成中文回答。"""
        parts: list[str] = []
        for tool, payload in observations:
            text = self._render_one(tool, payload)
            if text:
                parts.append(text)
        if not parts:
            return self._fallback_text(question)
        return "\n".join(parts)

    def _render_one(self, tool: str, payload: dict[str, Any]) -> str:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if payload.get("ok") is False:
            return str(payload.get("summary") or payload.get("message") or "这一步没有成功。")
        data = data or {}
        summary = payload.get("summary")

        if tool == "search_books":
            items = data.get("items") or []
            total = data.get("total", len(items))
            if not items:
                return f"没有检索到相关书目（共 {total} 条）。可以换个关键词，或者告诉我书名/作者我来帮您查。"
            lines = [f"共检索到 {total} 条，为您列出前 {len(items)} 条："]
            for idx, item in enumerate(items, 1):
                title = item.get("title", "未知书名")
                author = item.get("author") or "佚名"
                available = item.get("availableCopies", item.get("available", "?"))
                location = item.get("shelfLocation") or item.get("location") or "位置待查"
                lines.append(f"{idx}. 《{title}》 {author}｜可借 {available} 册｜{location}")
            if data.get("degraded"):
                reason = data.get("degradeReason") or data.get("degrade_reason") or ""
                lines.append(
                    f"（检索服务当前处于降级模式{('：' + str(reason)) if reason else ''}，结果可能不完整）"
                )
            return "\n".join(lines)

        if tool == "get_stock":
            title = data.get("title", "该书")
            available = data.get("available", 0)
            # available == -1 是后端"降级：库存无法确认"的约定值，
            # 必须说成"暂时查不到"，绝不能说成"没有库存/借不到"
            if available is None or int(available) < 0:
                return (
                    f"《{title}》的库存现在查不到（图书馆系统处于降级状态），"
                    "我无法确认能不能借到。请稍后再问我一次，或到 1 楼服务台/自助查询机确认。"
                )
            total = data.get("total", "?")
            queue = data.get("queueLen", 0)
            shelf = data.get("shelfLocation") or "位置待查"
            text = f"《{title}》馆藏 {total} 册，当前可借 {available} 册，索书位置：{shelf}。"
            if not available:
                text += f" 目前没有可借复本，预约队列已有 {queue} 人，需要我帮您预约吗？"
            return text

        if tool == "borrow":
            due = data.get("dueDate") or data.get("due_date")
            return f"借阅已办理成功，借阅记录号 {data.get('borrowId', '-')}，请于 {due} 前归还。"

        if tool == "renew":
            return f"续借成功，新的应还日期是 {data.get('newDueDate', '-')}。"

        if tool == "my_loans":
            # my_loans 工具返回 {"items": [...], "count": n}；也兼容直接给列表的后端
            loans = data.get("items") if isinstance(data, dict) else data
            if loans is None:
                loans = data if isinstance(data, list) else []
            if not loans:
                return "您当前没有在借图书。" if not data.get("overdueCount") else "没有符合条件的记录。"
            lines = [f"您当前有 {len(loans)} 条在借记录："]
            for item in loans:
                title = item.get("title", "未知书名")
                due = item.get("dueDate") or item.get("due_date") or "-"
                overdue = item.get("overdueDays") or 0
                renewable = "可续借" if item.get("renewable") else "不可续借"
                flag = f"，已逾期 {overdue} 天" if overdue else ""
                lines.append(f"· 《{title}》应还 {due}{flag}（{renewable}）")
            return "\n".join(lines)

        if tool == "get_reader_profile":
            rtype = data.get("readerTypeLabel") or data.get("readerType", "未知")
            credit = data.get("creditScore", "-")
            count = data.get("borrowedCount", 0)
            cats = data.get("preferredCategories") or []
            quota = data.get("quota") or {}
            cat_text = "、".join(cats) if cats else "暂无"
            text = (
                f"您是{rtype}，信用分 {credit}，当前在借 {count} 册，常借分类：{cat_text}。"
            )
            if quota:
                remaining = max(0, int(quota.get("maxBooks", 0)) - int(count))
                text += (
                    f" 您这类读者可借 {quota.get('maxBooks')} 册、借期 {quota.get('loanDays')} 天、"
                    f"可续借 {quota.get('maxRenew')} 次，目前还可借 {remaining} 册。"
                )
            if data.get("canBorrow") is False:
                text += " 提示：信用分低于 60，暂时不能借阅新书（可以查询和预约）。"
            return text

        if tool == "policy_qa":
            answer = data.get("answer") or data.get("summary") or ""
            source = data.get("source") or ""
            suffix = f"（来源：{source}）" if source else ""
            if answer:
                return f"{answer}{suffix}"
            chunks = data.get("chunks") or []
            if chunks:
                return f"{chunks[0].get('text', '')}{suffix}"
            return summary or "我在知识库里没有找到这条规则的明确说明，建议咨询馆员。"

        if tool == "recommend":
            items = data.get("items") or data.get("list") or []
            if not items:
                return "暂时没有找到合适的推荐，您可以说一个感兴趣的主题，我按主题推荐。"
            lines = [f"根据您的偏好为您推荐 {len(items)} 本："]
            for item in items:
                lines.append(f"· 《{item.get('title', '未知书名')}》{item.get('author') or ''}")
            return "\n".join(lines)

        # 兜底：有 summary 就用 summary，否则给一句保守回答
        return str(summary) if summary else "已为您查询到结果。"


__all__ = ["MockLLM", "KNOWN_BOOKS", "INTENT_TO_TOOL", "extract_book_title", "mock_plan"]
