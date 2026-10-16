"""评测用的**进程内模拟后端**。

为什么评测需要一个模拟后端（而不是"没有后端就跑评测"）：
`python -m app.eval.run_eval --provider mock` 的定位是**离线验证 Agent 自身逻辑**
（意图、工具选择、参数、编排、RAG、护栏）。如果强行连真实 Java 后端，
在没有后端的环境里所有工具都会失败，端到端成功率会直接是 0——
那个 0 反映的是"后端没开"，而不是"Agent 不行"，指标就失去了意义。

因此评测默认 `--backend fake`：用这个内存后端喂出**结构正确、内容可控**的数据，
让指标衡量的是 Agent 的决策质量。要验收真实联调时用 `--backend live`，
报告里会如实标注用的是哪一种后端（`backend` 字段），不会把两者混为一谈。

[注意] 这里的数据只用于评测，不是业务事实来源；真实数据一律来自 Java 后端。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

#: 评测用书目表（book_id 与 mock LLM 的 KNOWN_BOOKS 对应）
#: 评测用书目表。**键就是 book_id（后端主键语义）**，值里不再重复存 id。
#: id 取真实后端风格的小数字（实测：算法导论=5、计算机网络=3），
#: 这样“运行时到底用了哪个 id”才是可断言的。
BOOKS: dict[int, dict[str, Any]] = {
    5: {
        "title": "算法导论",
        "author": "Thomas H. Cormen",
        "publisher": "机械工业出版社",
        "publishYear": 2013,
        "category": "计算机",
        "callNumber": "TP301.6/C822",
        "availableCopies": 3,
        "totalCopies": 5,
        "shelfLocation": "3 楼中文科技图书区 TP301.6/C822",
    },
    3: {
        "title": "计算机网络",
        "author": "谢希仁",
        "publisher": "电子工业出版社",
        "publishYear": 2017,
        "category": "计算机",
        "callNumber": "TP393/X475",
        "availableCopies": 0,
        "totalCopies": 4,
        "shelfLocation": "3 楼中文科技图书区 TP393/X475",
    },
    4: {
        "title": "高等数学",
        "author": "同济大学数学系",
        "publisher": "高等教育出版社",
        "publishYear": 2014,
        "category": "数学",
        "callNumber": "O13/T437",
        "availableCopies": 6,
        "totalCopies": 10,
        "shelfLocation": "3 楼中文科技图书区 O13/T437",
    },
    6: {
        "title": "深入理解计算机系统",
        "author": "Randal E. Bryant",
        "publisher": "机械工业出版社",
        "publishYear": 2016,
        "category": "计算机",
        "callNumber": "TP303/B915",
        "availableCopies": 2,
        "totalCopies": 6,
        "shelfLocation": "3 楼中文科技图书区 TP303/B915",
    },
    7: {
        "title": "Java 并发编程实战",
        "author": "Brian Goetz",
        "publisher": "机械工业出版社",
        "publishYear": 2012,
        "category": "计算机",
        "callNumber": "TP312JA/G597",
        "availableCopies": 1,
        "totalCopies": 3,
        "shelfLocation": "3 楼中文科技图书区 TP312JA/G597",
    },
    12: {
        "title": "人类简史",
        "author": "尤瓦尔·赫拉利",
        "publisher": "中信出版社",
        "publishYear": 2014,
        "category": "历史",
        "callNumber": "K02/H432",
        "availableCopies": 4,
        "totalCopies": 5,
        "shelfLocation": "4 楼中文文学与历史区 K02/H432",
    },
    10: {
        "title": "机器学习",
        "author": "周志华",
        "publisher": "清华大学出版社",
        "publishYear": 2016,
        "category": "计算机",
        "callNumber": "TP181/Z785",
        "availableCopies": 2,
        "totalCopies": 8,
        "shelfLocation": "3 楼中文科技图书区 TP181/Z785",
    },
}

#: 读者借阅记录
LOANS: dict[int, list[dict[str, Any]]] = {
    1001: [
        {
            "borrowId": 5001,
            "bookId": 5,
            "title": "算法导论",
            "borrowAt": "2026-01-30",
            "dueDate": "2026-03-01",
            "renewCount": 0,
            "renewable": True,
            "overdueDays": 0,
            "fine": 0,
            "status": "BORROWED",
        },
        {
            "borrowId": 5002,
            "bookId": 3,
            "title": "计算机网络",
            "borrowAt": "2025-12-06",
            "dueDate": "2026-01-05",
            "renewCount": 2,
            "renewable": False,
            "overdueDays": 12,
            "fine": 2.4,
            "status": "OVERDUE",
        },
    ],
    1002: [
        {
            "borrowId": 6001,
            "bookId": 4,
            "title": "高等数学",
            "borrowAt": "2026-02-01",
            "dueDate": "2026-04-02",
            "renewCount": 0,
            "renewable": True,
            "overdueDays": 0,
            "fine": 0,
            "status": "BORROWED",
        }
    ],
}

#: 读者档案
PROFILES: dict[int, dict[str, Any]] = {
    1001: {
        "readerId": 1001,
        "readerType": "UNDERGRAD",
        "creditScore": 92,
        "preferredCategories": ["计算机", "数学"],
        "borrowedCount": 2,
        "status": "NORMAL",
    },
    1002: {
        "readerId": 1002,
        "readerType": "POSTGRAD",
        "creditScore": 100,
        "preferredCategories": ["数学"],
        "borrowedCount": 1,
        "status": "NORMAL",
    },
}

#: 规则库直答（真实环境里来自后端 rule_config 表）
POLICY_ANSWERS: dict[str, dict[str, str]] = {
    "本科生能借几本": {"answer": "本科生可借 10 册，借期 30 天，可续借 2 次。", "source": "rule_config:borrow.limit.UNDERGRAD"},
    "图书馆几点开门": {"answer": "学期内 2-4 楼 08:00-22:00 开放，自助借还机 24 小时。", "source": "rule_config:open.hours"},
}


class EvalBackend:
    """httpx.MockTransport 用的内存后端。"""

    def __init__(self, *, strict_sign: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        #: 评测里默认关闭签名校验：签名协议由 tests/test_signature.py 用固定向量单独验证，
        #: 这里开着只会引入"时间戳/编码"这类与 Agent 决策无关的噪声。
        self.strict_sign = strict_sign
        self.borrowed: set[tuple[int, int]] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # ---------------- 路由 ----------------
    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = request.url.query.decode("utf-8") if request.url.query else ""
        body: dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content.decode("utf-8"))
            except ValueError:
                body = {}
        self.calls.append({"method": request.method, "path": path, "body": body, "query": query})

        if path == "/internal/agent/tools/search-books":
            return self._ok(self._search(str(body.get("query") or "")))
        if path.endswith("/stock") and "/books/" in path:
            book_id = self._int_from_path(path)
            book = BOOKS.get(book_id)
            if not book:
                # 真实后端对不存在的书目返回业务失败 code=2002
                # （系统走不到这里，因为 book_id 已由检索结果背书；保留它以覆盖异常路径）
                return self._ok(None)
            return self._ok(
                {
                    "bookId": book_id,
                    "title": book["title"],
                    "available": book["availableCopies"],
                    "total": book["totalCopies"],
                    "queueLen": 0 if book["availableCopies"] else 3,
                    "shelfLocation": book["shelfLocation"],
                    "callNumber": book["callNumber"],
                    "degraded": False,
                }
            )
        if path == "/internal/agent/tools/borrow":
            reader_id = int(body.get("readerId") or 0)
            book_id = int(body.get("bookId") or 0)
            book = BOOKS.get(book_id)
            if not book:
                return self._ok({"ok": False, "reason": "BOOK_NOT_FOUND"})
            if book["availableCopies"] <= 0:
                return self._ok({"ok": False, "reason": "NO_AVAILABLE_COPY"})
            if (reader_id, book_id) in self.borrowed:
                return self._ok({"ok": False, "reason": "DUPLICATE_BORROW"})
            self.borrowed.add((reader_id, book_id))
            return self._ok({"ok": True, "borrowId": 9000 + len(self.borrowed), "dueDate": "2026-04-15", "reason": ""})
        if path == "/internal/agent/tools/renew":
            borrow_id = int(body.get("borrowId") or 0)
            for loans in LOANS.values():
                for loan in loans:
                    if loan["borrowId"] == borrow_id:
                        if not loan["renewable"]:
                            return self._ok({"ok": False, "reason": "RENEW_NOT_ALLOWED_RESERVED"})
                        return self._ok({"ok": True, "newDueDate": "2026-05-01", "reason": ""})
            return self._ok({"ok": False, "reason": "BORROW_RECORD_NOT_FOUND"})
        if path.endswith("/loans") and "/readers/" in path:
            reader_id = self._int_from_path(path)
            return self._ok(LOANS.get(reader_id, []))
        if path.endswith("/profile") and "/readers/" in path:
            reader_id = self._int_from_path(path)
            return self._ok(PROFILES.get(reader_id))
        if path == "/internal/agent/tools/reservations":
            return self._ok({"ok": True, "queueNo": 4, "reason": ""})
        if path == "/internal/agent/tools/policy":
            topic = ""
            for pair in query.split("&"):
                if pair.startswith("topic="):
                    from urllib.parse import unquote

                    topic = unquote(pair[len("topic=") :])
            for key, value in POLICY_ANSWERS.items():
                if key in topic:
                    return self._ok(dict(value))
            # 未命中规则库 → 返回空，触发 Agent 的本地 RAG 降级路径（这正是我们要评的）
            return self._ok({"answer": "", "source": ""})
        if path.endswith("/similar") and "/books/" in path:
            book_id = self._int_from_path(path)
            seed = BOOKS.get(book_id) or next(iter(BOOKS.values()))
            items = [
                self._as_item(bid, b)
                for bid, b in BOOKS.items()
                if bid != book_id and b["category"] == seed["category"]
            ]
            return self._ok(items[:3])
        if path == "/internal/agent/tools/books/hot":
            return self._ok([self._as_item(4, BOOKS[4]), self._as_item(6, BOOKS[6])])
        return self._ok(None)

    # ---------------- 辅助 ----------------
    @staticmethod
    def _as_item(book_id: int, meta: dict[str, Any]) -> dict[str, Any]:
        """把书目条目序列化成后端返回的形状（含 bookId）。

        BOOKS 的键就是主键，值里不再重复存 id；这里在出口补回 bookId，
        保证"工具结果里的 id 就是后端主键"这个语义成立。
        """
        return {"bookId": book_id, **meta}

    def _search(self, query: str) -> dict[str, Any]:
        keyword = (query or "").strip()
        if keyword:
            items = [
                self._as_item(bid, book)
                for bid, book in BOOKS.items()
                if keyword in book["title"] or keyword in book["author"] or keyword in book["category"]
            ]
        else:
            items = []
        if not items and keyword:
            # 退一步做逐字匹配，模拟倒排索引的部分召回
            items = [
                self._as_item(bid, book)
                for bid, book in BOOKS.items()
                if any(ch in book["title"] for ch in keyword if "\u4e00" <= ch <= "\u9fff")
            ][:3]
        return {
            "items": items[:5],
            "total": len(items),
            "tookMs": 12,
            "engine": "inverted-index",
            "degraded": False,
        }

    @staticmethod
    def _int_from_path(path: str) -> int:
        for part in path.split("/"):
            if part.isdigit():
                return int(part)
        return 0

    @staticmethod
    def _ok(data: Any) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 0, "msg": "ok", "data": data, "traceId": "tr-eval-backend"}
        )

    # ---------------- 供指标使用 ----------------
    def reset(self) -> None:
        self.calls.clear()
        self.borrowed.clear()

    def book_ids(self) -> set[int]:
        return set(BOOKS)


__all__ = ["EvalBackend", "BOOKS", "LOANS", "PROFILES"]
