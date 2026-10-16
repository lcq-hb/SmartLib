"""记忆测试：会话滑动窗口、TTL、待确认写操作、读者画像。

这些用例是"记忆真的在起作用"的证据，而不只是"类能实例化"：
- 滑动窗口必须真的丢老消息（maxlen 生效）；
- TTL 过期后必须查不到（否则内存会无界增长）；
- pending 有过期时间（否则三天后回一句"确认"还能借书）；
- 画像要能被借阅记录反哺（偏好分类随对话积累）。
"""

from __future__ import annotations

import time

import pytest

from app.memory.reader_profile import ReaderProfile, ReaderProfileStore
from app.memory.session_store import PendingAction, Session, SessionStore


# =============================================================================
# 会话
# =============================================================================
def test_new_session_gets_unique_id() -> None:
    store = SessionStore()
    first = store.get_or_create(None)
    second = store.get_or_create(None)
    assert first.session_id != second.session_id
    assert first.session_id.startswith("s-")


def test_same_session_id_returns_same_session() -> None:
    store = SessionStore()
    session = store.get_or_create("s-fixed", reader_id=1001)
    again = store.get_or_create("s-fixed")
    assert again is session
    assert again.reader_id == 1001


def test_reader_id_is_updated_when_provided_again() -> None:
    store = SessionStore()
    session = store.get_or_create("s1")
    assert session.reader_id is None
    store.get_or_create("s1", reader_id=1007)
    assert session.reader_id == 1007


def test_history_is_a_sliding_window(test_settings) -> None:
    store = SessionStore(test_settings)
    session = store.get_or_create("s-win")
    for i in range(30):
        session.add_user(f"问题{i}")
        session.add_assistant(f"回答{i}")
    window = session.history(test_settings.session_max_turns)
    assert len(window) == test_settings.session_max_turns
    # 窗口里必须是最新的那几条
    assert window[-1].content == "回答29"


def test_history_hard_cap_prevents_unbounded_growth() -> None:
    session = Session(session_id="s")
    for i in range(200):
        session.add_user(f"q{i}")
    assert len(session.turns) <= 40


def test_history_zero_limit_returns_empty() -> None:
    session = Session(session_id="s")
    session.add_user("x")
    assert session.history(0) == []


def test_tool_message_keeps_name_and_call_id() -> None:
    session = Session(session_id="s")
    session.add_tool("get_stock", '{"ok":true}', "call_1")
    msg = session.turns[-1]
    assert msg.role == "tool"
    assert msg.name == "get_stock"
    assert msg.tool_call_id == "call_1"


# =============================================================================
# TTL 与容量
# =============================================================================
def test_expired_session_is_garbage_collected(test_settings) -> None:
    """TTL 过期的会话必须被惰性清理（否则内存无界增长）。

    实现细节：直接改写 updated_at 而不是把 TTL 设成 0。
    time.time() 的浮点精度下，"刚创建就取"算出的时间差可能是 0，
    让 `diff > ttl` 恰好不成立——测试就会变成偶发失败。改写时间戳是确定性的。
    """
    store = SessionStore(test_settings)
    store.get_or_create("s-old")
    session = store.get("s-old")
    assert session is not None
    session.updated_at -= test_settings.session_ttl_seconds + 10
    assert store.get("s-old") is None


def test_session_store_hits_and_misses_counted() -> None:
    store = SessionStore()
    store.get_or_create("s1")
    store.get_or_create("s1")
    stats = store.stats()
    assert stats["sessions"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_session_store_evicts_beyond_capacity(monkeypatch) -> None:
    store = SessionStore()
    monkeypatch.setattr(SessionStore, "MAX_SESSIONS", 3)
    for i in range(6):
        store.get_or_create(f"s{i}")
    stats = store.stats()
    assert stats["sessions"] <= 3
    assert stats["evictions"] >= 1


def test_delete_and_clear() -> None:
    store = SessionStore()
    store.get_or_create("s1")
    assert store.delete("s1") is True
    assert store.get("s1") is None
    store.get_or_create("s2")
    store.clear()
    assert store.stats()["sessions"] == 0


# =============================================================================
# 待确认写操作
# =============================================================================
def test_pending_action_roundtrip() -> None:
    session = Session(session_id="s")
    session.set_pending("borrow", {"readerId": 1001, "bookId": 5})
    assert session.pending is not None
    action = session.pop_pending()
    assert action is not None
    assert action.tool == "borrow"
    assert action.args["bookId"] == 5
    # pop 之后必须被清空，防止同一句"确认"被执行两次
    assert session.pending is None


def test_pending_action_expires() -> None:
    action = PendingAction(tool="borrow", args={}, created_at=time.time() - 10_000)
    assert action.expired is True
    session = Session(session_id="s")
    session.pending = action
    assert session.pop_pending() is None


def test_pending_action_describe_is_human_readable() -> None:
    assert "书目 5" in PendingAction(tool="borrow", args={"bookId": 5}).describe()
    assert "记录 5001" in PendingAction(tool="renew", args={"borrowId": 5001}).describe()


def test_clear_pending() -> None:
    session = Session(session_id="s")
    session.set_pending("borrow", {})
    session.clear_pending()
    assert session.pending is None


# =============================================================================
# 读者画像
# =============================================================================
@pytest.mark.asyncio
async def test_profile_store_returns_none_for_unknown_reader(test_settings) -> None:
    store = ReaderProfileStore(test_settings)
    assert await store.get(1001) is None
    assert await store.get(None) is None


@pytest.mark.asyncio
async def test_profile_upsert_from_backend(test_settings) -> None:
    store = ReaderProfileStore(test_settings)
    profile = await store.upsert_from_backend(
        1001,
        {
            "readerType": "POSTGRAD",
            "creditScore": 88,
            "preferredCategories": ["计算机"],
            "borrowedCount": 4,
        },
    )
    assert profile.reader_type == "POSTGRAD"
    assert profile.credit_score == 88
    assert profile.preferred_categories == ["计算机"]

    cached = await store.get(1001)
    assert cached is not None
    assert cached.borrowed_count == 4


@pytest.mark.asyncio
async def test_profile_learns_categories_from_loans(test_settings) -> None:
    store = ReaderProfileStore(test_settings)
    await store.observe_loans(
        1001,
        [
            {"title": "算法导论", "category": "计算机"},
            {"title": "机器学习", "category": "计算机"},
            {"title": "高等数学", "category": "数学"},
        ],
    )
    profile = await store.get(1001)
    assert profile is not None
    # 出现最多的分类排最前（这是"偏好"的定义）
    assert profile.preferred_categories[0] == "计算机"
    assert "算法导论" in profile.recent_books
    assert profile.borrowed_count == 3


@pytest.mark.asyncio
async def test_profile_recent_books_are_deduplicated_and_capped(test_settings) -> None:
    store = ReaderProfileStore(test_settings)
    for i in range(15):
        await store.observe_loans(1001, [{"title": f"书{i}", "category": "计算机"}])
    await store.observe_loans(1001, [{"title": "书0", "category": "计算机"}])
    profile = await store.get(1001)
    assert profile is not None
    assert len(profile.recent_books) <= 10
    assert len(set(profile.recent_books)) == len(profile.recent_books)


@pytest.mark.asyncio
async def test_profile_ttl_expiry(test_settings) -> None:
    """同上：用改写时间戳的方式测 TTL，避免浮点精度导致的偶发失败。"""
    store = ReaderProfileStore(test_settings)
    await store.upsert_from_backend(1001, {"readerType": "TEACHER"})
    cached = await store.get(1001)
    assert cached is not None
    # get() 返回的是副本，TTL 判断读的是内部对象，因此直接改内部表
    store._profiles[1001].updated_at -= test_settings.profile_ttl_seconds + 10
    assert await store.get(1001) is None


@pytest.mark.asyncio
async def test_profile_get_returns_copy_not_shared_reference(test_settings) -> None:
    """取出来的画像被改坏不能影响缓存（并发读写安全）。"""
    store = ReaderProfileStore(test_settings)
    await store.upsert_from_backend(
        1001, {"readerType": "TEACHER", "creditScore": 90, "preferredCategories": ["数学"]}
    )
    first = await store.get(1001)
    assert first is not None
    first.preferred_categories.append("被改坏了")
    second = await store.get(1001)
    assert second is not None
    assert "被改坏了" not in second.preferred_categories


@pytest.mark.asyncio
async def test_set_categories_explicitly(test_settings) -> None:
    store = ReaderProfileStore(test_settings)
    profile = await store.set_categories(1001, ["文学", "文学", "历史"])
    assert profile.preferred_categories[0] == "文学"
    assert profile.category_counter["文学"] == 2


def test_profile_to_dict_shape() -> None:
    profile = ReaderProfile(reader_id=1001, reader_type="UNDERGRAD", credit_score=90)
    payload = profile.to_dict()
    assert payload["readerId"] == 1001
    assert payload["readerType"] == "UNDERGRAD"
    assert "preferredCategories" in payload


@pytest.mark.asyncio
async def test_profile_stats(test_settings) -> None:
    store = ReaderProfileStore(test_settings)
    await store.upsert_from_backend(1001, {"readerType": "TEACHER"})
    await store.get(1001)
    stats = store.stats()
    assert stats["profiles"] == 1
    assert stats["writes"] == 1
    assert stats["hits"] == 1
