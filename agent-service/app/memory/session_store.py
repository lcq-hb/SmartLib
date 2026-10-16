"""会话短期记忆：滑动窗口 + TTL。

为什么是"滑动窗口"而不是"全量历史 + 摘要"：
一次图书馆咨询通常 3~6 轮就结束，全量历史会让 prompt 线性膨胀，
而多出来的历史大多是已完成话题（问完在几楼又问罚金），对当前轮没有增益。
保留最近 N 轮（默认 10）足够覆盖"上一次说了什么书"这类指代消解需求。

为什么要 TTL：
内存实现如果不清理，长跑进程就是内存泄漏。TTL 默认 30 分钟，
与读者一次咨询的时长量级匹配；同时用"惰性清理 + 上限淘汰"两级策略，
避免引入后台线程（那会带来 FastAPI 生命周期管理复杂度）。

无 Redis 时的进程内实现：多实例部署时记忆不共享。
这是**有意的取舍**并在 README 里写明局限：要共享就换 Redis 实现，
接口（get/append/set_pending/pop_pending）保持不变。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.llm.base import ChatMessage

#: 待确认的写操作：LLM 提出 → 护栏拦下 → 用户确认后放行
PENDING_TTL_SECONDS = 300


@dataclass
class PendingAction:
    """等待用户二次确认的写操作。"""

    tool: str
    args: dict[str, Any]
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > PENDING_TTL_SECONDS

    def describe(self) -> str:
        if self.tool == "borrow":
            return f"借阅书目 {self.args.get('bookId')}"
        if self.tool == "renew":
            return f"续借借阅记录 {self.args.get('borrowId')}"
        return f"执行 {self.tool}"


@dataclass
class Session:
    """一个会话的状态。"""

    session_id: str
    reader_id: int | None = None
    turns: deque[ChatMessage] = field(default_factory=lambda: deque(maxlen=40))
    pending: PendingAction | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: 同一句话的意图缓存：读者连着问同一句时不用重复跑 LLM
    intent_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = time.time()

    def add_user(self, text: str) -> None:
        self.turns.append(ChatMessage(role="user", content=text))
        self.touch()

    def add_assistant(self, text: str) -> None:
        self.turns.append(ChatMessage(role="assistant", content=text))
        self.touch()

    def add_tool(self, name: str, content: str, tool_call_id: str) -> None:
        self.turns.append(
            ChatMessage(role="tool", content=content, name=name, tool_call_id=tool_call_id)
        )
        self.touch()

    def history(self, limit: int) -> list[ChatMessage]:
        """最近 limit 条消息（滑动窗口）。

        deque(maxlen=40) 做硬上限防止单会话内存失控，limit 是软窗口（默认 10 轮对话）。
        """
        items = list(self.turns)
        return items[-limit:] if limit > 0 else []

    def set_pending(self, tool: str, args: dict[str, Any]) -> PendingAction:
        self.pending = PendingAction(tool=tool, args=dict(args))
        self.touch()
        return self.pending

    def pop_pending(self) -> PendingAction | None:
        action = self.pending
        self.pending = None
        if action is not None and action.expired:
            return None
        return action

    def clear_pending(self) -> None:
        self.pending = None


class SessionStore:
    """进程内会话存储（线程安全）。"""

    #: 最多同时保留多少个会话，超出按最久未使用淘汰（单实例演示足够）
    MAX_SESSIONS = 2000

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    # ---------------- 基础操作 ----------------
    def new_session_id(self) -> str:
        return f"s-{uuid.uuid4().hex[:16]}"

    def get_or_create(
        self, session_id: str | None, *, reader_id: int | None = None
    ) -> Session:
        sid = session_id or self.new_session_id()
        with self._lock:
            self._gc_locked()
            session = self._sessions.get(sid)
            if session is None:
                session = Session(session_id=sid, reader_id=reader_id)
                self._sessions[sid] = session
                self.misses += 1
            else:
                self.hits += 1
                if reader_id is not None:
                    session.reader_id = reader_id
            self._sessions.move_to_end(sid)
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            self._gc_locked()
            return self._sessions.get(session_id)

    def save(self, session: Session) -> None:
        with self._lock:
            session.touch()
            self._sessions[session.session_id] = session
            self._sessions.move_to_end(session.session_id)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    # ---------------- 清理 ----------------
    def _gc_locked(self) -> None:
        """惰性清理：TTL 过期 + 超出容量。在持锁状态下调用。"""
        ttl = self.settings.session_ttl_seconds
        now = time.time()
        # 用严格大于：TTL=0 时任何会话都算过期（配置 0 的语义是"不缓存"）
        expired = [sid for sid, s in self._sessions.items() if now - s.updated_at > ttl]
        for sid in expired:
            self._sessions.pop(sid, None)
        while len(self._sessions) > self.MAX_SESSIONS:
            self._sessions.popitem(last=False)
            self.evictions += 1

    # ---------------- 只读视图 ----------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._gc_locked()
            return {
                "sessions": len(self._sessions),
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "ttl_seconds": self.settings.session_ttl_seconds,
                "window_turns": self.settings.session_max_turns,
                "pending_actions": sum(1 for s in self._sessions.values() if s.pending),
            }


#: 进程级单例：FastAPI 的 /chat 与评测脚本共用，保证"同一 session_id 有记忆"可被测试
_SESSION_STORE: SessionStore | None = None


def get_session_store(settings: Settings | None = None) -> SessionStore:
    global _SESSION_STORE
    if _SESSION_STORE is None:
        _SESSION_STORE = SessionStore(settings)
    return _SESSION_STORE


def reset_session_store() -> None:
    """测试用：清空并重建单例。"""
    global _SESSION_STORE
    _SESSION_STORE = None


__all__ = [
    "Session",
    "SessionStore",
    "PendingAction",
    "get_session_store",
    "reset_session_store",
    "PENDING_TTL_SECONDS",
]
