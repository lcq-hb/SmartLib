"""policy_qa：借阅规则问答（RAG）。

对应蓝图 §8.2：GET /internal/agent/tools/policy?topic=... → {answer,source}
（规则库直答，Agent 的 RAG 数据源之一）

三级数据源，逐级降级，保证"规则问题一定答得出来"：
1. **后端规则库直答**（最权威，规则改动立即生效）—— `java.policy(topic)`；
2. **本地知识库 RAG**（BM25 + 向量混合召回，可离线）—— `Retriever.retrieve()`；
3. **纯知识库首句直答**（连索引都没建好时的兜底）—— `Retriever.answer_hint()`。

为什么不是"只用 RAG"：蓝图把 `/policy` 定义为"RAG 数据源之一"，
说明规则类问题的**权威答案是配置化的规则库**，知识库文档是它的可读投影。
两者冲突时以后端为准（本工具把后端结果放在第一优先级，并在 data 里标出 source）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.client.java_client import JavaClient
from app.errors import SmartLibError
from app.rag.retriever import get_ready_retriever
from app.tool_schemas import PolicyQaArgs
from app.tools.base import Tool, ToolContext, ToolError, ToolResult


class PolicyQaTool(Tool):
    name = "policy_qa"
    description = (
        "回答图书馆规章制度类问题：可借册数、借期、续借次数、逾期罚金、预约与到馆保留、"
        "开放时间、馆藏楼层分布、学科服务、办证与账号等。"
        "当读者问'规则是什么''怎么算''在几楼''几点开馆'时使用。"
        "不返回具体某本书的库存或读者个人数据。"
    )
    args_model = PolicyQaArgs
    is_write = False
    requires_reader = False
    #: 规则问答有明确出处（source），算作有证据的回答
    provides_evidence = True

    async def handle(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = PolicyQaArgs.model_validate(args)

        # ---- 第 1 级：后端规则库直答 ----
        answer, source, upstream_error = await self._try_upstream(params, ctx)
        if answer:
            return ToolResult(
                ok=True,
                tool=self.name,
                data={"answer": answer, "source": source, "retrieval": "rule_engine"},
                summary=f"规则库直答（来源 {source}）",
            )

        # ---- 第 2 级：本地 RAG ----
        rag_result = await self._try_rag(params, ctx)
        if rag_result is not None:
            if upstream_error:
                rag_result["degraded"] = True
                rag_result["degraded_reason"] = upstream_error
            return ToolResult(
                ok=True,
                tool=self.name,
                data=rag_result,
                summary=f"知识库检索到 {len(rag_result['chunks'])} 条依据（来源 {rag_result['source']}）",
            )

        raise ToolError(
            f"知识库没有命中 topic={params.topic}",
            user_message=(
                "我在知识库里没有找到这条规则的明确说明。"
                "建议您在图书馆主页查看《流通管理办法》，或咨询 1 楼服务台，我也可以帮您登记问题转交馆员。"
            ),
            code="POLICY_NOT_FOUND",
        )

    async def _try_upstream(
        self, params: PolicyQaArgs, ctx: ToolContext
    ) -> tuple[str, str, str | None]:
        java: JavaClient | None = ctx.java
        if java is None:
            return "", "", "未注入 JavaClient"
        try:
            data = await java.policy(topic=params.topic, trace_id=ctx.trace_id)
        except SmartLibError as exc:
            # 规则库不可用不能让规则问答整体失能 —— 记下原因，走本地 RAG
            return "", "", f"{exc.code}: {exc.detail}"
        except Exception as exc:  # noqa: BLE001
            return "", "", f"{type(exc).__name__}: {exc}"

        answer = str(data.get("answer") or "").strip()
        source = str(data.get("source") or "规则库").strip()
        return answer, source, None

    async def _try_rag(self, params: PolicyQaArgs, ctx: ToolContext) -> dict[str, Any] | None:
        retriever = await get_ready_retriever(ctx.settings)
        hits = await retriever.retrieve(params.topic, top_k=params.top_k)
        hits = [h for h in hits if self._relevant(params.topic, h.chunk.text, retriever)]
        if hits:
            chunks = [
                {
                    "text": hit.chunk.text,
                    "source": f"{hit.chunk.source}#{hit.chunk.breadcrumb}",
                    "score": round(hit.score, 4),
                    "matched_by": hit.matched_by,
                }
                for hit in hits
            ]
            return {
                "answer": hits[0].chunk.text,
                "source": f"{hits[0].chunk.source}#{hits[0].chunk.breadcrumb}",
                "chunks": chunks,
                "retrieval": "hybrid_bm25_vector",
                "degraded": False,
            }

        # ---- 第 3 级：纯 BM25 首句直答 ----
        hint = retriever.answer_hint(params.topic)
        if hint and self._relevant(params.topic, hint, retriever):
            return {
                "answer": hint,
                "source": "知识库（降级检索）",
                "chunks": [{"text": hint, "source": "knowledge", "score": 0.0}],
                "retrieval": "bm25_only",
                "degraded": True,
            }
        return None

    #: 相关性闸门：IDF 加权覆盖率（查询里"有信息量"的词有多少出现在了片段中）。
    #: 阈值是**在本项目知识库上实测标定**的（28 条应命中的规则问句 + 14 条域外问句）：
    #: - ratio >= 0.20：域外问句只漏 1 条（"食堂几点开饭"——与"图书馆几点开门"
    #:   共享"几点"这种具体词，属于词面巧合，纯词法检索无法区分）；
    #: - 收紧到 0.30：域外问句 0 漏，但 3 条正常问句会被判"没查到"
    #:   （如"预约到馆保留几天"），读者体验损失更大。
    #: 取舍：宁可漏 1 条域外问句，也不要让读者频繁听到"没查到"。
    #: 注：曾同时要求"高 IDF 主题词必须命中"，但实测对多主题问句过于苛刻
    #: （"预约到馆保留几天"里最高 IDF 的词在答案片段里就是不出现），已去掉该条件。
    MIN_RELEVANCE_RATIO = 0.20

    @classmethod
    def _relevant(cls, query: str, text: str, retriever: Any) -> bool:
        stats = retriever.relevance_weight(query, text)
        return stats["ratio"] >= cls.MIN_RELEVANCE_RATIO


TOOL = PolicyQaTool()
