"""recommend：图书推荐。

蓝图 §8.2 的 8 条内部接口里没有单列推荐，因为它可以由已有数据组合出来，
所以这里实现一个**多策略 + 逐级降级**的推荐工具：

1. **有种子书目**（读者说"和《算法导论》类似的书"）→ similar_books（后端相似度）
2. **有分类 / 或从读者画像取偏好分类** → hot_books(category)（该类热门）
3. **兜底** → hot_books()（全馆热门榜）

为什么不做"纯 LLM 推荐"：LLM 不知道馆藏里到底有什么书，
编出来的书名借不到，只会让读者白跑一趟。推荐必须由馆藏数据驱动，
LLM 只负责**解释推荐理由**（这一层在 runtime 里做）。

推荐理由（reason）是确定性拼装的，不依赖 LLM：
评测要能断言"推荐结果非空且有理由"，把理由交给 LLM 会让指标不稳定。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.client.java_client import JavaClient
from app.errors import SmartLibError
from app.tool_schemas import RecommendArgs
from app.tools.base import Tool, ToolContext, ToolError, ToolResult


class RecommendTool(Tool):
    name = "recommend"
    description = (
        "为读者推荐图书。可传 book_id（以某本书为种子推荐同类书）或 category（按分类推荐）；"
        "都不传时按读者常借分类与全馆热门榜推荐。"
        "当读者问'有什么好书推荐''类似这本书的还有哪些''我该看什么'时使用。"
    )
    args_model = RecommendArgs
    is_write = False
    requires_reader = False

    async def handle(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        params = RecommendArgs.model_validate(args)
        java: JavaClient | None = ctx.java
        if java is None:
            raise ToolError("未注入 JavaClient", user_message="系统未正确初始化，请联系管理员。")

        strategy = ""
        errors: list[str] = []
        items: list[dict[str, Any]] = []
        category = params.category

        # ---- 策略 2 的前置：从读者画像补出偏好分类 ----
        if not category and params.book_id is None and ctx.reader_id:
            category = await self._preferred_category(ctx)
            if category:
                strategy = "reader_profile_category"

        # ---- 策略 1：种子书相似推荐 ----
        if params.book_id:
            try:
                raw = await java.similar_books(
                    book_id=params.book_id, limit=params.limit, trace_id=ctx.trace_id
                )
                items = self._normalize(raw, reason_prefix="与种子书目同类")
                if items:
                    strategy = "similar_books"
            except SmartLibError as exc:
                errors.append(f"similar: {exc.code}")

        # ---- 策略 2：分类热门 ----
        if not items and category:
            try:
                raw = await java.hot_books(
                    limit=params.limit, category=category, trace_id=ctx.trace_id
                )
                items = self._normalize(raw, reason_prefix=f"{category} 类热门")
                if items:
                    strategy = strategy or "category_hot"
            except SmartLibError as exc:
                errors.append(f"category_hot: {exc.code}")

        # ---- 策略 3：全馆热门兜底 ----
        if not items:
            try:
                raw = await java.hot_books(limit=params.limit, category=None, trace_id=ctx.trace_id)
                items = self._normalize(raw, reason_prefix="全馆热门")
                if items:
                    strategy = "hot_fallback"
            except SmartLibError as exc:
                errors.append(f"hot: {exc.code}")

        if not items:
            raise ToolError(
                f"推荐数据不可用: {'; '.join(errors) or '无数据'}",
                user_message=(
                    "推荐服务暂时不可用，我没法确认馆藏里有哪些可推荐的书。"
                    "您可以告诉我感兴趣的主题，我换个方式帮您找。"
                ),
                code="RECOMMEND_UNAVAILABLE",
            )

        degraded = bool(errors)
        return ToolResult(
            ok=True,
            tool=self.name,
            data={
                "items": items[: params.limit],
                "strategy": strategy,
                "category": category,
                "degraded": degraded,
                "errors": errors,
            },
            summary=f"按策略 {strategy} 推荐 {len(items[: params.limit])} 本",
        )

    @staticmethod
    async def _preferred_category(ctx: ToolContext) -> str | None:
        """读取长期画像里的偏好分类（不额外打后端，画像本身有 TTL 缓存）。"""
        try:
            from app.memory.reader_profile import get_profile_store

            profile = await get_profile_store(ctx.settings).get(ctx.reader_id)
            if profile and profile.preferred_categories:
                return profile.preferred_categories[0]
        except Exception:  # noqa: BLE001 - 画像拿不到不影响推荐主链路
            return None
        return None

    @staticmethod
    def _normalize(raw: list[dict[str, Any]], *, reason_prefix: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or item.get("name")
            if not title:
                continue
            available = item.get("availableCopies", item.get("available_copies"))
            category = item.get("category") or item.get("categoryName") or ""
            items.append(
                {
                    "bookId": item.get("bookId") or item.get("book_id") or item.get("id"),
                    "title": title,
                    "author": item.get("author") or "",
                    "category": category,
                    "available": available,
                    "reason": f"{reason_prefix}（{category}）" if category else reason_prefix,
                }
            )
        return items


TOOL = RecommendTool()
