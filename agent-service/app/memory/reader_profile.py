"""读者画像长期记忆：偏好分类、最近借阅、信用分快照。

为什么不每次推荐都去打后端 profile：
推荐是"顺手做的事"，如果每次推荐都串行多打一个后端接口，
既增加延迟又增加后端压力（推荐面板一次刷新可能触发多次）。
画像是**慢变量**（偏好分类一周内几乎不变），缓存 24 小时完全够用，
TTL 用 Settings.profile_ttl_seconds 可配。

更新时机（这是"记忆"能起作用的关键）：
- 每次成功调用 get_reader_profile → 用它刷新画像；
- 每次成功调用 my_loans → 用最近的借阅书目所属分类反哺偏好分类。
这样画像会随着对话自然积累，而不是只在首次问答时写一次。
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings


@dataclass
class ReaderProfile:
    """长期画像。字段与蓝图 §8.2 的 /profile 契约对齐，另加本地统计字段。"""

    reader_id: int
    reader_type: str = "UNDERGRAD"
    credit_score: int = 100
    preferred_categories: list[str] = field(default_factory=list)
    borrowed_count: int = 0
    #: 本地累计的分类命中次数，用于在没有后端画像时也能给出偏好
    category_counter: Counter[str] = field(default_factory=Counter)
    recent_books: list[str] = field(default_factory=list)
    last_borrow_at: str | None = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "readerId": self.reader_id,
            "readerType": self.reader_type,
            "creditScore": self.credit_score,
            "preferredCategories": self.preferred_categories,
            "borrowedCount": self.borrowed_count,
            "recentBooks": self.recent_books[:5],
            "lastBorrowAt": self.last_borrow_at,
            "updatedAt": self.updated_at,
        }

    def observe_categories(self, categories: list[str]) -> None:
        """累积观察到借阅分类，并据此刷新偏好（取前 3）。"""
        for category in categories:
            if category:
                self.category_counter[category] += 1
        if self.category_counter:
            self.preferred_categories = [
                name for name, _ in self.category_counter.most_common(3)
            ]

    def observe_books(self, titles: list[str]) -> None:
        for title in titles:
            if title and title not in self.recent_books:
                self.recent_books.insert(0, title)
        self.recent_books = self.recent_books[:10]


class ReaderProfileStore:
    """进程内画像存储（TTL + 容量上限，与 session_store 同样的清理策略）。"""

    MAX_PROFILES = 5000

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._profiles: dict[int, ReaderProfile] = {}
        self._lock = threading.Lock()
        self.writes = 0
        self.hits = 0
        self.misses = 0

    async def get(self, reader_id: int | None) -> ReaderProfile | None:
        if reader_id is None:
            return None
        with self._lock:
            profile = self._profiles.get(reader_id)
            if profile is None:
                self.misses += 1
                return None
            if time.time() - profile.updated_at > self.settings.profile_ttl_seconds:
                # 严格大于：TTL=0 表示"不缓存画像"
                self._profiles.pop(reader_id, None)
                self.misses += 1
                return None
            self.hits += 1
            # 返回**深一层**的副本：preferred_categories 是可变 list，
            # 只做 ReaderProfile(**__dict__) 这种浅拷贝依然共享同一个 list 对象，
            # 调用方 append 一下就把缓存改坏了（实测踩过）。
            return ReaderProfile(
                reader_id=profile.reader_id,
                reader_type=profile.reader_type,
                credit_score=profile.credit_score,
                preferred_categories=list(profile.preferred_categories),
                borrowed_count=profile.borrowed_count,
                category_counter=profile.category_counter.copy(),
                recent_books=list(profile.recent_books),
                last_borrow_at=profile.last_borrow_at,
                updated_at=profile.updated_at,
            )

    async def upsert_from_backend(self, reader_id: int, data: dict[str, Any]) -> ReaderProfile:
        """用后端 /profile 的返回刷新画像。"""
        with self._lock:
            profile = self._profiles.get(reader_id) or ReaderProfile(reader_id=reader_id)
            profile.reader_type = str(data.get("readerType") or profile.reader_type)
            profile.credit_score = int(data.get("creditScore", profile.credit_score) or 0)
            backend_cats = list(data.get("preferredCategories") or [])
            if backend_cats:
                profile.preferred_categories = backend_cats
                for category in backend_cats:
                    profile.category_counter[category] += 1
            profile.borrowed_count = int(data.get("borrowedCount", profile.borrowed_count) or 0)
            profile.updated_at = time.time()
            self._profiles[reader_id] = profile
            self.writes += 1
            self._gc_locked()
            return profile

    async def observe_loans(self, reader_id: int, loans: list[dict[str, Any]]) -> ReaderProfile:
        """用借阅记录反哺画像：借了什么分类的书 → 更新偏好。"""
        with self._lock:
            profile = self._profiles.get(reader_id) or ReaderProfile(reader_id=reader_id)
            categories = [str(item.get("category") or "") for item in loans]
            profile.observe_categories([c for c in categories if c])
            profile.observe_books([str(item.get("title") or "") for item in loans])
            profile.borrowed_count = len(loans)
            profile.updated_at = time.time()
            self._profiles[reader_id] = profile
            self.writes += 1
            self._gc_locked()
            return profile

    async def set_categories(self, reader_id: int, categories: list[str]) -> ReaderProfile:
        """显式设置偏好（读者说"我喜欢看计算机的书"时用）。"""
        with self._lock:
            profile = self._profiles.get(reader_id) or ReaderProfile(reader_id=reader_id)
            profile.observe_categories(categories)
            profile.updated_at = time.time()
            self._profiles[reader_id] = profile
            self.writes += 1
            return profile

    def _gc_locked(self) -> None:
        ttl = self.settings.profile_ttl_seconds
        now = time.time()
        expired = [rid for rid, p in self._profiles.items() if now - p.updated_at > ttl]
        for rid in expired:
            self._profiles.pop(rid, None)
        while len(self._profiles) > self.MAX_PROFILES:
            oldest = min(self._profiles.items(), key=lambda kv: kv[1].updated_at)[0]
            self._profiles.pop(oldest, None)

    def clear(self) -> None:
        with self._lock:
            self._profiles.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "profiles": len(self._profiles),
                "writes": self.writes,
                "hits": self.hits,
                "misses": self.misses,
                "ttl_seconds": self.settings.profile_ttl_seconds,
            }


_PROFILE_STORE: ReaderProfileStore | None = None


def get_profile_store(settings: Settings | None = None) -> ReaderProfileStore:
    global _PROFILE_STORE
    if _PROFILE_STORE is None:
        _PROFILE_STORE = ReaderProfileStore(settings)
    return _PROFILE_STORE


def reset_profile_store() -> None:
    global _PROFILE_STORE
    _PROFILE_STORE = None


__all__ = [
    "ReaderProfile",
    "ReaderProfileStore",
    "get_profile_store",
    "reset_profile_store",
]
