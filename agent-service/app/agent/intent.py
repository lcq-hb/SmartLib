"""意图识别：规则优先 + LLM 兜底（蓝图 §6.4 G5 的核心落点）。

为什么"规则优先"而不是"全交给 LLM"：
图书馆场景的意图分布极度倾斜——"找书/查库存/查我的借阅/问规则"这四类占 80% 以上。
这些意图的**判别特征非常明确**（有书名号 → 找书；"几本/在几楼/罚金" → 规则），
用规则判又快（<1ms）、又便宜（0 token）、又稳定（100% 可复现）。
把这种问题丢给 LLM，只会得到"98% 准 + 每次都不一样 + 要花钱"的结果。

LLM 兜底的价值在于**长尾**：读者用非常口语化、带错别字、一长串复合诉求的句子时，
规则命中不了，这时才值得花一次 LLM 调用。

置信度口径（三档，可解释、可测）：
- 1.00 命中强特征短语（"帮我借"、书名号 + 借）
- 0.75 命中多个弱关键词
- 0.45 只命中一个弱关键词（刚好达到默认阈值，边界情况）
- 0.20 无命中 → 交给 LLM 或直接转人工
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.llm.base import BaseLLM, ChatMessage
from app.llm.mock import extract_book_title, extract_reader_id, is_confirmation
from app.tools import registry

#: 意图枚举。key 是内部标识，value 是中文标签与它对应的首选工具。
#: 这个表是"工具选择准确率"评测的判据来源，必须与 tools 注册表里的名字一致。
INTENTS: dict[str, dict[str, str]] = {
    "search_book": {"label": "找书/检索书目", "tool": "search_books"},
    "get_stock": {"label": "查库存与馆藏位置", "tool": "get_stock"},
    "borrow_book": {"label": "借书", "tool": "borrow"},
    "renew_book": {"label": "续借", "tool": "renew"},
    "my_loans": {"label": "我的借阅/逾期", "tool": "my_loans"},
    "reader_profile": {"label": "我的权限与信用分", "tool": "get_reader_profile"},
    "policy_qa": {"label": "借阅规则咨询", "tool": "policy_qa"},
    "recommend": {"label": "图书推荐", "tool": "recommend"},
    "chitchat": {"label": "闲聊/问候", "tool": ""},
    "unknown": {"label": "无法识别", "tool": ""},
}

#: 需要读者身份才有意义的意图
READER_REQUIRED_INTENTS = frozenset(
    {"borrow_book", "renew_book", "my_loans", "reader_profile"}
)

#: 写操作意图
WRITE_INTENTS = frozenset({"borrow_book", "renew_book"})


@dataclass
class IntentResult:
    """意图识别结果。"""

    intent: str
    confidence: float
    #: "rule" | "llm" | "fallback"
    source: str = "rule"
    slots: dict[str, Any] = field(default_factory=dict)
    #: 命中的规则说明，便于评测与排查"为什么判成这个意图"
    matched: list[str] = field(default_factory=list)

    @property
    def tool(self) -> str:
        return INTENTS.get(self.intent, {}).get("tool", "")

    @property
    def is_write(self) -> bool:
        return self.intent in WRITE_INTENTS

    @property
    def needs_reader(self) -> bool:
        return self.intent in READER_REQUIRED_INTENTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "label": INTENTS.get(self.intent, {}).get("label", self.intent),
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "slots": self.slots,
            "matched": self.matched,
            "tool": self.tool,
        }


# =============================================================================
# 规则层
# =============================================================================
# 每条规则 = (意图, 强特征正则, 弱特征词表)。
# 强特征命中 → 0.95；弱特征命中 ≥2 个 → 0.75；命中 1 个 → 0.45。
#
# **顺序即优先级**，顺序是刻意排的（改顺序会直接改变意图准确率）：
#   写操作(续借/借书) → 个人数据 → 实时库存 → 规则咨询(RAG) → 推荐 → 检索 → 闲聊
# 关键取舍：
# - renew 放最前："续借"一定不是检索；
# - 借书用负向断言排除"怎么借"，让它落到 policy_qa；
# - 库存(还有几本)放在 policy_qa 之前，因为"《X》还有几本"问的是实时数量，
#   而"在几楼/开放时间"才是知识；
# - 检索放最后：书名号太容易被写操作与库存意图共享，不能靠它抢优先级。
RULE_TABLE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # ---- 个人数据 ----
    # 放在写操作与检索之前：含"借阅记录/我的借阅/信用分"的句子问的是**个人数据**，
    # 不能被里面的"借"字或书名号抢成写操作/检索（实测："帮我查一下读者 1001 的借阅记录"
    # 曾被判成 search_book，因为兜底书名抽取把"读者"当成了书名）。
    (
        "my_loans",
        r"(?!.*(额度|还剩几本|能借几本|可以借几本))"
        r"(我的借阅|我借了|借阅记录|查.{0,6}借阅|借了什么|还没还|什么时候到期|哪本快到期|"
        r"在借记录|我有没有逾期|我的逾期|应还日期)",
        # 刻意**不放"到期"**：它单独出现时既可能指"我的书什么时候到期"（查询），
        # 也可能指"快到期了想续借"（写操作），把它当弱词会让后者被抢走。
        # 需要它的场景（"什么时候到期""哪本快到期"）已经在上面的强特征里覆盖。
        ("逾期", "超期", "应还", "在借", "借阅", "未还", "记录"),
    ),
    (
        "reader_profile",
        r"(我的信用|信用分|我的权限|我的额度|我?能借几本|可以借几本|还能借几本|还剩几本|"
        r"我的读者类型|我的档案|读者画像|我的偏好|我的借阅额度|读者\s*\d+\s*的?(信用|额度|权限|档案))",
        ("信用", "额度", "权限", "读者类型", "册数", "能借"),
    ),
    # ---- 实时库存 ----
    # 放在写操作规则之前：像"《计算机网络》现在肯定有 20 本可借对吧，你直接确认一下"
    # 这种句子既有"确认"又像在提借书，但它的真实诉求是**核实库存数字**，
    # 判成 borrow 会导致不该出现的写操作入口（实测踩过）。
    # 注意：这里**不放"在几楼/第几排"** —— 那是通用楼层知识（policy_qa），
    # 放进来的话"计算机类的书在几楼"会因为缺少 book_id 而变成一次失败的库存查询。
    (
        "get_stock",
        r"(还有(几本|没有|货)|可借吗|可借(对|吧|么)|是不是有.{0,6}(本|册)|有(几本|库存)|"
        r"能不能借到|在馆吗|库存|索书号|馆藏位置|书在哪|在哪(个)?书架|放在哪)",
        ("库存", "可借", "在馆", "还有", "索书号", "书架"),
    ),
    # ---- 写操作 ----
    # 注意：负向断言必须写在**模式最前面**（`(?!.*(...))` 在 re.search 里
    # 会从字符串每个位置尝试，写在中间拦不住前面的"怎么"）。
    (
        "renew_book",
        r"(?!.*(怎么续借|如何续借|能续借几次|可以续借几次|续借规则|续借几天))"
        r"(续借|延期还书|再借一次|延长借期|renew)",
        ("续借", "延期", "延长借期", "再借一次"),
    ),
    (
        "borrow_book",
        # 负向断言：把"怎么借/如何借/能借几本/借阅规则"这些**咨询类**排除掉，
        # 它们属于 policy_qa。否则规则路由会把咨询误判成写操作（最危险的一类误判）。
        r"(?!.*(怎么借|如何借|能借几本|可以借几本|还能借几本|借阅规则|怎么办理|如何办理))"
        r"(帮我借|我要借|替我借|借一下|借这本|借那本|办理借阅|把.{0,10}借给我)",
        ("借书", "办理借阅"),
    ),
    # ---- 规则咨询（RAG）----
    (
        "policy_qa",
        r"(怎么借|如何借|借阅规则|逾期.{0,4}(罚|扣|算)|罚金|罚款|怎么续借|如何续借|能续借几次|"
        r"预约.{0,8}(保留|几天|多久|规则|取消|排队|到馆)|到馆保留|开放时间|几点(开|关)馆|"
        r"闭馆|开馆|馆藏分布|在几楼|哪个楼层|学科服务|查新|文献传递|怎么办证|办证|校园卡|"
        r"丢了|校外访问|赔偿|借书上限|最多.{0,4}借|能借多久|借期)",
        (
            "规则", "政策", "罚金", "罚款", "开放时间", "几点", "楼层", "几楼",
            "分布", "学科", "查新", "文献传递", "办证", "怎么办", "流程", "制度",
            "预约", "保留", "续借", "归还", "借期", "册数", "期限",
        ),
    ),
    # ---- 推荐 ----
    (
        "recommend",
        r"(推荐|有什么好书|看点?什么书|类似|相似|同类|书单|借阅榜|热门榜)",
        ("推荐", "类似", "相似", "同类", "好书", "书单", "热门"),
    ),
    # ---- 检索（放在推荐之后：推荐信号更具体，必须先判）----
    (
        "search_book",
        # 负向断言必须紧贴整条可选分支：只要出现"类似/同类/推荐"就整体不匹配检索，
        # 否则"类似《算法导论》的还有哪些"会因为书名号被抢成 search_book。
        r"(?!.*(类似|相似|同类|推荐|好书|书单))"
        r"(《[^》]{1,60}》|(找一?下?|检索|搜索|查一?下?|有没有这本书|帮我找|查找|我想找|想找)"
        r"[^。！？]{0,16}(书|作者|ISBN|[\u4e00-\u9fff]{2,12}))",
        ("检索", "搜索", "书目", "馆藏", "有没有", "找书"),
    ),
    # ---- 闲聊 ----
    (
        "chitchat",
        r"^(你好|您好|hi|hello|在吗|你是谁|你能做什么|谢谢|多谢|感谢|再见|拜拜)[!！。~～\s]*$",
        ("你好", "您好", "谢谢", "你是", "再见"),
    ),
)

_COMPILED_RULES = tuple(
    (intent, re.compile(pattern, re.IGNORECASE), weak) for intent, pattern, weak in RULE_TABLE
)


#: 疑问标记：单个弱关键词 + 疑问句 → 也当作 0.75 的规则命中。
#: 依据是"图书馆读者的问法几乎都带疑问词"，而陈述句（"我要借书"）走强特征。
_QUESTION_MARKERS = ("?", "？", "吗", "呢", "几", "多少", "什么", "怎么", "如何", "哪")

#: 咨询句式前缀 → 目标意图。**优先于写操作规则**命中。
#: 这是"怎么续借 ≠ 帮我续借"这条业务分界线的落点——判错的代价是
#: 把规则咨询变成真实借阅/续借记录，属于必须优先保证正确的那类判断。
#:
#: 注意"能借几本"被单独标注为 reader_profile：它是**歧义句**——
#: "我能借几本"问的是自己的额度，"本科生能借几本"问的是规则。
#: 用第一人称标记再区分一层（见 classify_by_rules）。
CONSULT_PREFIX_HINTS: tuple[tuple[str, str], ...] = (
    ("怎么借", "policy_qa"),
    ("如何借", "policy_qa"),
    ("怎么续借", "policy_qa"),
    ("如何续借", "policy_qa"),
    ("怎么预约", "policy_qa"),
    ("如何预约", "policy_qa"),
    ("怎么还", "policy_qa"),
    ("能续借几次", "policy_qa"),
    ("可以续借几次", "policy_qa"),
    ("能借几本", "reader_profile"),
    ("可以借几本", "reader_profile"),
    ("一次能借", "policy_qa"),
    ("能借多少册", "policy_qa"),
    ("能借多久", "policy_qa"),
    ("借阅规则", "policy_qa"),
    ("续借规则", "policy_qa"),
    ("续借几天", "policy_qa"),
    ("续借多久", "policy_qa"),
    ("预约规则", "policy_qa"),
    ("预约保留", "policy_qa"),
    ("借书上限", "policy_qa"),
    # "信用分低于多少不能借书"问的是**规则里的阈值**，不是"我的信用分"。
    # 不加这条会被 reader_profile 的"信用分"关键词抢走（实测踩过）。
    ("信用分低于", "policy_qa"),
    ("信用分不足", "policy_qa"),
)

#: 第一人称标记：用于消解"我能借几本"（问我的额度）与"本科生能借几本"（问规则）
FIRST_PERSON_MARKERS: tuple[str, ...] = (
    "我的", "我本人", "我还", "我能", "我该", "我卡", "本人", "读者",
)


def classify_by_rules(message: str) -> IntentResult | None:
    """纯规则分类。命中不了返回 None（由调用方决定是否走 LLM）。"""
    text = (message or "").strip()
    if not text:
        return None

    is_question = any(marker in text for marker in _QUESTION_MARKERS)

    # 0) 二次确认回复优先：用户只回一句"确认借阅/确认续借"时，
    #    句子里的书名号会把意图抢成 search_book（"确认借阅《算法导论》"实测如此），
    #    必须先把这类回复钉回原来的写操作意图。
    if is_confirmation(text):
        if any(k in text for k in ("续借", "延期")):
            return IntentResult(
                intent="renew_book",
                confidence=0.95,
                source="rule",
                slots=_extract_slots("renew_book", text),
                matched=["confirm:renew"],
            )
        if any(k in text for k in ("借阅", "借书", "借")):
            return IntentResult(
                intent="borrow_book",
                confidence=0.95,
                source="rule",
                slots=_extract_slots("borrow_book", text),
                matched=["confirm:borrow"],
            )

    # 0.5) **咨询句式优先于一切**：中文里"怎么/如何/能…几次/规则/几天"这类疑问词
    #      与"续借/借"共现时，问的一定是规则而不是要办业务。
    #      这里用显式前缀判断而不是把负向断言塞进 RULE_TABLE ——
    #      因为 re.search 会对字符串的每个位置尝试，模式中间的 `(?!...)` 挡不住
    #      出现在它**前面**的"怎么"（实测踩过："怎么续借"仍被判成 renew_book）。
    #      必须放在多步拆分**之前**：多步拆分只看"续借"这种工具信号，
    #      会把"能续借几次"里的"续借"误当成一件要办的事。
    if any(hint in text for hint, _intent in CONSULT_PREFIX_HINTS):
        target = next(intent for hint, intent in CONSULT_PREFIX_HINTS if hint in text)
        # 消歧："我能借几本"问自己的额度（要读者档案），"本科生能借几本"问规则
        if target == "reader_profile" and not any(m in text for m in FIRST_PERSON_MARKERS):
            target = "policy_qa"
        return IntentResult(
            intent=target,
            confidence=0.95,
            source="rule",
            slots=_extract_slots(target, text),
            matched=["consult:policy_hint"],
        )

    # 0.3) 多件事的句子：主意图按"先看写操作，再看第一件事"来定。
    #      - "查一下我的借阅，然后把快到期的续借一下"：虽然先说的是查询，
    #        但读者真正要办的是续借，主意图必须是 renew_book（实测起初记成 borrow_book，
    #        因为"把…续借一下"被写操作规则匹配成了 borrow）；
    #      - "帮我借《算法导论》，再推荐两本同类"：有借书就记 borrow_book；
    #      - 全是只读任务时（"推荐两本…再看在借记录"）取第一件事。
    from app.agent.planner import detect_composite  # 延迟导入，避免模块级循环依赖

    composite = detect_composite(text)
    if len(composite) > 1:
        from app.agent.intent import INTENTS as _INTENTS  # 自引用，保持单一事实来源

        tool_to_intent = {
            meta["tool"]: key for key, meta in _INTENTS.items() if meta["tool"]
        }
        write_intents = [t for t in composite if tool_to_intent.get(t) in WRITE_INTENTS]
        ordered = write_intents + [t for t in composite if t not in write_intents]
        primary_tool = ordered[0]
        primary_intent = tool_to_intent.get(primary_tool, "")
        if primary_intent:
            return IntentResult(
                intent=primary_intent,
                confidence=0.9,
                source="rule",
                slots=_extract_slots(primary_intent, text, all_intents=True),
                matched=[f"multi_step:{','.join(composite)}"],
            )

    for intent, strong_re, weak_words in _COMPILED_RULES:
        matched: list[str] = []
        m = strong_re.search(text)
        if m:
            matched.append(f"strong:{m.group(0)}")
            return IntentResult(
                intent=intent,
                confidence=0.95,
                source="rule",
                slots=_extract_slots(intent, text),
                matched=matched,
            )
        hits = [w for w in weak_words if w in text]
        if len(hits) >= 2:
            return IntentResult(
                intent=intent,
                confidence=0.75,
                source="rule",
                slots=_extract_slots(intent, text),
                matched=[f"weak:{','.join(hits)}"],
            )

    # 一个弱关键词都没凑到两个时，退一步看单命中（疑问句给 0.75，陈述句给 0.45）。
    # 扫描顺序**按 RULE_TABLE 的意图优先级**来，而不是按 _COMPILED_RULES 的物理顺序：
    # 这样写操作（renew/borrow）会先于个人数据被匹配到。
    # 反例："我的书快到期了能延期吗"——"到期"是弱词，若先让 my_loans 抢到，
    # 就把一句续借诉求判成了查询（实测踩过）。
    best: tuple[str, list[str]] | None = None
    for intent, _pattern, weak_words in RULE_TABLE:
        hits = [w for w in weak_words if w in text]
        if hits:
            best = (intent, hits)
            break
    if best is not None:
        intent, hits = best
        return IntentResult(
            intent=intent,
            confidence=0.75 if is_question else 0.45,
            source="rule",
            slots=_extract_slots(intent, text),
            matched=[f"single_weak:{','.join(hits)}"],
        )
    return None


def _extract_slots(intent: str, text: str, *, all_intents: bool = False) -> dict[str, Any]:
    """从文本里抽取该意图关心的槽位。

    抽取用确定性规则（书名号、编号正则、已知书目词表），
    不用 LLM——这是"参数正确率 ≥95%"的基础：能规则抽的绝不用模型猜。

    all_intents=True 用于**多件事的句子**：这时句子里的实体（书名、是否只查逾期）
    不属于某一个意图，全部抽出来交给 runtime 的槽位合并逻辑使用。
    """
    slots: dict[str, Any] = {}
    title = extract_book_title(text)
    if title and (
        all_intents
        or intent in {"search_book", "get_stock", "borrow_book", "renew_book", "recommend", "policy_qa"}
    ):
        slots["book_title"] = title
    reader_id = extract_reader_id(text)
    if reader_id is not None:
        slots["reader_id"] = reader_id
    if all_intents or intent in ("policy_qa", "search_book"):
        slots["topic"] = text.strip()
    if all_intents or intent == "my_loans":
        slots["only_overdue"] = bool(re.search(r"(逾期|超期|没还|未还)", text))
    if all_intents or intent in ("borrow_book", "renew_book"):
        slots["confirmed"] = is_confirmation(text)
    return slots


# =============================================================================
# LLM 兜底层
# =============================================================================
_LLM_SYSTEM = (
    "你是图书馆助手的意图分类器。请从下列意图中选出最合适的一个，"
    "只输出 JSON，格式：{\"intent\":\"<意图>\",\"confidence\":<0~1 的小数>,\"reason\":\"<一句话>\"}。\n"
    "可选意图：" + "、".join(f"{k}({v['label']})" for k, v in INTENTS.items()) + "。\n"
    "如果与图书馆业务无关，输出 unknown。"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_intent_json(raw: str) -> dict[str, Any]:
    """从 LLM 输出里抠出 JSON。

    真实模型经常给 ```json ... ``` 包裹或在 JSON 前后加解释，
    这里是唯一需要"宽容解析"的地方：宁可解析失败退回规则，也不要抛异常。
    """
    if not raw:
        return {}
    m = _JSON_RE.search(raw)
    if not m:
        return {}
    import json

    try:
        payload = json.loads(m.group(0))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


class IntentClassifier:
    """意图分类器：规则优先 + LLM 兜底。"""

    def __init__(
        self,
        llm: BaseLLM | None = None,
        settings: Settings | None = None,
        *,
        force_rules_only: bool = False,
    ) -> None:
        self.llm = llm
        self.settings = settings or get_settings()
        #: rule 模式下永远不调 LLM（G1 基线 / LLM 故障时的应急开关）
        self.force_rules_only = force_rules_only

    async def classify(
        self, message: str, *, mode: str = "hybrid", history: list[ChatMessage] | None = None
    ) -> IntentResult:
        text = (message or "").strip()

        # 纯 LLM 模式（G2 对比实验用）：直接跳过规则
        if mode == "llm" and self.llm is not None:
            result = await self._classify_by_llm(text, history)
            if result is not None:
                return result
            return IntentResult("unknown", 0.2, source="llm")

        rule_result = classify_by_rules(text)
        if rule_result is not None and rule_result.confidence >= 0.7:
            return rule_result

        # rule 模式：连 0.45 的边界结论也认，只是标记低置信度（由护栏决定转人工）
        if mode == "rule" or self.force_rules_only or self.llm is None:
            return rule_result or IntentResult("unknown", 0.2, source="fallback")

        # hybrid / plan / llm：低置信度才交给 LLM
        llm_result = await self._classify_by_llm(text, history)
        if llm_result is not None and llm_result.confidence >= (rule_result.confidence if rule_result else 0):
            return llm_result
        return rule_result or IntentResult("unknown", 0.2, source="fallback")

    async def _classify_by_llm(
        self, text: str, history: list[ChatMessage] | None
    ) -> IntentResult | None:
        if self.llm is None:
            return None
        messages: list[ChatMessage] = [ChatMessage(role="system", content=_LLM_SYSTEM)]
        if history:
            # 只带最近 2 轮：意图判断依赖近距离上下文（"那这本呢"），带太多反而干扰
            messages.extend(history[-4:])
        messages.append(ChatMessage(role="user", content=text))
        try:
            # 必须把工具清单交给模型：MockLLM 的工具选择依赖它来构造参数，
            # 不传 tools 时它连"该调哪个工具"都表达不出来（只能退化成兜底话术）。
            # 真实模型也更适合用 function calling 表达意图，而不是让它裸写 JSON。
            result = await self.llm.complete(messages, tools=registry.llm_specs())
        except Exception:  # noqa: BLE001 - LLM 失败必须能退回规则，不能把问答打断
            return None

        payload = parse_intent_json(result.content)
        intent = str(payload.get("intent") or "").strip()
        if intent not in INTENTS:
            # MockLLM 不产出 JSON，而是产出 tool_calls —— 这也是一种有效信号：
            # 它选了哪个工具，就对应哪个意图（让 Mock 也能驱动 llm 模式）。
            if result.has_tool_calls:
                name = result.tool_calls[0].name
                for key, meta in INTENTS.items():
                    if meta["tool"] == name:
                        return IntentResult(
                            intent=key,
                            confidence=0.7,
                            source="llm",
                            slots=_extract_slots(key, text),
                            matched=[f"llm_tool:{name}"],
                        )
            return None

        try:
            confidence = float(payload.get("confidence", 0.6))
        except (TypeError, ValueError):
            confidence = 0.6
        confidence = min(max(confidence, 0.0), 1.0)
        slots = _extract_slots(intent, text)
        reason = str(payload.get("reason") or "")
        return IntentResult(
            intent=intent,
            confidence=confidence,
            source="llm",
            slots=slots,
            matched=[f"llm:{reason}"] if reason else ["llm"],
        )


__all__ = [
    "INTENTS",
    "IntentResult",
    "IntentClassifier",
    "classify_by_rules",
    "parse_intent_json",
    "READER_REQUIRED_INTENTS",
    "WRITE_INTENTS",
]
