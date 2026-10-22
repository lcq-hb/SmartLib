#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SmartLib 借阅接口压测脚本（纯标准库，零依赖）

测什么
    并发 POST /api/borrows，验证 §6.1 六种库存扣减策略在同一套请求下的表现：
      * QPS / P50 / P90 / P99 / 最大耗时
      * 错误率（按 HTTP 状态码与业务 code 分别统计）
      * 成功借出数（code == 0 的请求数）
      * 【超卖检查】每轮结束后 GET /api/books/{id}，看 availableCopies 是否 < 0
        —— 蓝图 §7 的硬指标是"超卖数 = 0"，这一项是唯一不能妥协的。

为什么要有 --cycles 与 --return-each-cycle（测量设计的核心）
    最初的版本有个致命缺陷：**六种策略依次跑，全都压同一本书**。
    第一轮就把复本借光了，后面几轮的失败原因是"库存没了 / 同一读者重复借"，
    于是一张"六策略对比表"里 QPS/P99 测的其实是"业务规则拒绝的速度"，
    策略之间根本不可比。这一点在追问下一定会被问穿。

    修法：每一轮（cycle）在开始前把目标书**重置到已知库存**，用纯公开 API 实现：
        借出 N 本 → 采集指标 → 对每一笔 successful borrow 调
        POST /api/borrows/{id}/return 归还 → availableCopies 回到初始值。
    归还同时会把借阅记录置为 RETURNED，于是"同一读者同一本书只能持 1 册"的约束
    也一并解除（规则只统计 BORROWED/OVERDUE），读者可在下一轮复用。
    因此每一轮都从同一个已知库存出发，指标才可比。

    前置依赖（必须满足，否则数字无意义）：
      1) 读者池必须是"干净"的：信用分 >= min_credit_score、无逾期、
         无欠费、status=ACTIVE。给一个信用分 55 的读者会每轮都产生固定失败。
      2) 每轮请求数应等于该书的复本数，且每个读者只借 1 本
         （--reader-mode distinct），这样成功数应当**恰好等于**该轮可用复本数：
         少了 = 误拒（策略把该成功的请求拒了），多了 = 超卖。
      3) 兜底判据（每轮都算，不依赖 1/2）：
         afterBorrowAvailable == beforeAvailable - successCount
         即"成功 n 次就恰好扣 n 册"，任何多扣/漏扣都会被这一条抓到。

不使用 --return-each-cycle 时的诚实声明
    多轮 + 不重置库存会被 CLI 直接拦住（必须显式加 --allow-unreset-stock），
    因为那种模式下各轮共享库存，指标不可横向比较，只能当"混合负载"参考。

【重要实测发现】为什么库存必须从"内部签名接口"读，不能从 GET /api/books/{id} 读
    GET /api/books/{id} 走的是多级缓存（Caffeine L1 5min / Redis L2 30min），
    而全后端**唯一**的 StockService.StockChangeListener 实现是
    InvertedIndexSearchEngine（只更新倒排索引），**没有任何代码在库存变化时失效书目缓存**。
    实测：对 book 14 并发借出 8 册（8 条 BORROWED 记录、对账 violations=0），
    紧接着 GET /api/books/14 仍然返回 availableCopies=8（旧值），
    直到缓存 TTL 过期才会自愈。
    因此压测脚本改用内部签名接口作为权威库存源：
        GET /internal/agent/tools/books/{id}/stock   （StockService.view -> mapper，直读库）
        Header: X-Internal-Timestamp + X-Internal-Sign(HMAC-SHA256)
        canonical = timestamp + "\n" + METHOD + "\n" + pathWithQuery + "\n" + bodyUtf8
    同时仍然读一次缓存接口，把两者的差异记进报告（cacheDiverged），
    这本身就是一条有价值的观测：读者会看到过期库存。
    这个后端缺陷不在本脚本的修复范围内（写入路径受限），已在 summary.md 里如实记录。

为什么用 http.client + threading 而不是压测框架
    评审机/CI 上没有 wrk/ab/locust；纯标准库能保证脚本在任何 Python 3.7+ 上直接跑。

【诚实边界】这不是专业压测工具：
    * 每个请求新建一条 TCP 连接（最坏情况口径），没有连接池复用；
      真实网关后端的连接是复用的，所以本脚本测出的 QPS 是【下界】。
    * 客户端本身（GIL + Python 线程）也可能成为瓶颈。
    * 样本量小的时候（例如每轮只有几十个请求）QPS/P99 抖动很大，
      报告里会写明每轮样本数与样本总数，不要拿小样本的 QPS 下结论。

用法
    # 干净的单轮：8 个干净读者并发抢一本 8 复本的书，期望恰好 8 成功、0 超卖
    python scripts/bench/load_borrow.py --url http://localhost:8080 \
        --book-id 14 --threads 8 --requests 8 --reader-mode distinct \
        --reader-ids 1,3,4,6,7,8,9,10 --label optimistic-db \
        --out scripts/bench/results/borrow_optimistic-db.json

    # 多轮（每轮借出后归还，库存回到初始）：样本量更大，指标更稳
    python scripts/bench/load_borrow.py --url http://localhost:8080 \
        --book-id 24 --threads 8 --requests 8 --cycles 5 --return-each-cycle \
        --reader-ids 1,3,4,6,7,8,9,10 --reader-mode distinct --label pessimistic-db

    # 不开启归还的"混合负载"模式（保留兼容，指标不可跨轮比较）
    python scripts/bench/load_borrow.py --url http://localhost:8080 \
        --book-id 5 --threads 50 --requests 300 --cycles 3 --allow-unreset-stock
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
import platform
import random as _random
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse


# =============================================================================
# 工具函数
# =============================================================================
def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def percentile(sorted_values, pct: float) -> float:
    """
    线性插值分位数。sorted_values 必须已升序。
    为什么不用 statistics.quantiles：它要求 n>=2 且算法口径不易解释；
    这里用最通用的 nearest-rank + 线性插值，结果和 wrk/JMeter 的 P99 口径接近。
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    fraction = rank - low
    return float(sorted_values[low]) * (1.0 - fraction) + float(sorted_values[high]) * fraction


def parse_base_url(raw: str):
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("--url 必须是 http:// 或 https:// 开头的地址")
    if not parsed.hostname:
        raise ValueError("--url 缺少主机名")
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "prefix": parsed.path.rstrip("/"),
    }


def http_request(target, method: str, path: str, body=None, headers=None, timeout=30.0):
    """
    发一个请求，返回 (status, payload_dict_or_None, raw_text, error_str)。
    每次调用新建连接并显式关闭：保证耗时统计里包含完整的三次握手，
    口径是"最坏情况"，不会低估真实延迟。
    """
    conn = None
    full_path = target["prefix"] + path
    try:
        if target["scheme"] == "https":
            conn = http.client.HTTPSConnection(target["host"], target["port"], timeout=timeout)
        else:
            conn = http.client.HTTPConnection(target["host"], target["port"], timeout=timeout)
        conn.request(method, full_path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        status = response.status
        payload = None
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, payload, raw, None
    except Exception as error:  # 网络层错误：连接被拒/超时/读错误
        return 0, None, "", "{}: {}".format(type(error).__name__, error)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def extract_available(data: dict):
    """从书目详情里取可用库存，兼容 camelCase / snake_case / 嵌套 stock 三种写法。"""
    if not isinstance(data, dict):
        return None
    for key in ("availableCopies", "available_copies", "available"):
        value = data.get(key)
        if isinstance(value, int):
            return value
    stock = data.get("stock")
    if isinstance(stock, dict):
        for key in ("availableCopies", "available_copies", "available"):
            value = stock.get(key)
            if isinstance(value, int):
                return value
    return None


def parse_reader_ids(raw: str):
    """
    解析读者池：支持 "1" / "1,2,3" / "1-20" 三种写法，可混用（逗号分隔）。
    为什么需要读者池：业务规则 §2.2-5 规定"同一读者对同一 book_id 最多持有 1 册"，
    单个读者第二次借同一本书必然失败 —— 那是规则生效，不是策略失败。
    """
    if raw is None or str(raw).strip() == "":
        return [1]
    ids = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                low = int(bounds[0])
                high = int(bounds[1])
            except ValueError:
                raise ValueError("读者 id 区间格式错误：{}".format(part))
            if high < low:
                raise ValueError("读者 id 区间起止颠倒：{}".format(part))
            if high - low > 100000:
                raise ValueError("读者 id 区间过大（上限 10 万）：{}".format(part))
            ids.extend(range(low, high + 1))
        else:
            try:
                ids.append(int(part))
            except ValueError:
                raise ValueError("读者 id 必须是整数：{}".format(part))
    if not ids:
        raise ValueError("--reader-ids 解析后为空")
    # 去重但保持顺序：同一读者出现两次会让"每读者只借 1 本"的前提失效
    seen = set()
    unique = []
    for reader in ids:
        if reader not in seen:
            seen.add(reader)
            unique.append(reader)
    return unique


# =============================================================================
# 线程安全的读者分配器
# =============================================================================
class ReaderPool:
    """
    按顺序把读者分给请求。

    round-robin：第 i 个请求用 readers[i % n]（旧行为，会重复用到同一个读者）
    distinct   ：先打乱，然后每个读者只用一次；用完了重新打乱再来一轮，
                 并把"重复启用轮次"记进报告（readerReuseRounds）。
                 distinct 是"成功数应当恰好等于复本数"这一判据的前提：
                 若允许重复使用同一读者，重复借同一本书必然失败，
                 失败原因就变成业务规则而不是策略差异了。
    """

    def __init__(self, reader_ids, mode: str, seed_label: str):
        self.reader_ids = list(reader_ids)
        self.mode = mode
        self.lock = threading.Lock()
        self.cursor = 0
        self.reuse_rounds = 0
        self.used = {}          # readerId -> 本次 cycle 内使用次数
        self.order = list(self.reader_ids)
        if mode == "distinct":
            # 用 label 做种子：不同 label 的读者顺序不同，
            # 避免"每次都是同一批读者先上"造成的系统性偏差
            digest = hashlib.md5(seed_label.encode("utf-8")).hexdigest()[:8]
            self.rng = _random.Random(int(digest, 16))
            self.rng.shuffle(self.order)

    def next_reader(self) -> int:
        with self.lock:
            if self.mode == "distinct":
                if self.cursor >= len(self.order):
                    # 读者用完了：重新洗牌再来一轮。
                    # 正常场景不该发生（requests 应 <= 干净读者数）；
                    # 发生了就说明请求数配多了，报告里会体现 readerReuseRounds。
                    self.reuse_rounds += 1
                    self.rng.shuffle(self.order)
                    self.cursor = 0
                reader = self.order[self.cursor]
                self.cursor += 1
            else:
                reader = self.order[self.cursor % len(self.order)]
                self.cursor += 1
            self.used[reader] = self.used.get(reader, 0) + 1
            return reader

    def reset_usage(self):
        """每个 cycle 重置计数器（保留 shuffle 顺序，跨轮读者分布自然轮换）。"""
        with self.lock:
            self.cursor = 0
            self.reuse_rounds = 0
            self.used = {}


# =============================================================================
# 压测主体
# =============================================================================
class BorrowBenchmark:
    def __init__(self, args):
        self.args = args
        self.target = parse_base_url(args.url)
        self.reader_ids = parse_reader_ids(args.reader_ids)
        self.lock = threading.Lock()
        self.pool = ReaderPool(self.reader_ids, args.reader_mode, args.label)
        # 结果收集：所有线程往共享列表里追加，加锁保护
        self.latencies = []          # 每个请求的耗时(ms)，跨 cycle 汇总
        self.cycle_latencies = []    # 当前 cycle 的耗时
        self.status_counts = {}
        self.success = 0
        self.business_fail = 0
        self.http_fail = 0
        self.network_fail = 0
        self.errors = []             # 最多留 --max-error-samples 条样本
        self.sample_messages = []
        self.cycle_borrow_ids = []   # 当前 cycle 成功借出的 borrowId（用于归还）
        self.per_thread_requests = 0
        # 跨 cycle 累计值。注意：record() 里的 success/... 会被 reset_cycle_counters()
        # 清零（cycle 记录需要的是"本轮的"数），汇总必须另用一套累加器，
        # 否则 result.successCount 会只剩最后一个 cycle 的数字（踩过）。
        self.all_success = 0
        self.all_business_fail = 0
        self.all_http_fail = 0
        self.all_network_fail = 0
        self.all_status_counts = {}
        self.all_messages = []

    # ---------------------------------------------------------------- 记录
    def reset_cycle_counters(self):
        with self.lock:
            self.status_counts = {}
            self.success = 0
            self.business_fail = 0
            self.http_fail = 0
            self.network_fail = 0
            self.sample_messages = []
            self.cycle_borrow_ids = []
            self.cycle_latencies = []
        self.pool.reset_usage()

    def record(self, latency_ms, status, code, message, borrow_id):
        with self.lock:
            self.latencies.append(latency_ms)
            self.cycle_latencies.append(latency_ms)
            code_key = "{} (code={})".format(status if status else "network-error",
                                             code if code is not None else "-")
            self.status_counts[code_key] = self.status_counts.get(code_key, 0) + 1
            self.all_status_counts[code_key] = self.all_status_counts.get(code_key, 0) + 1
            if status == 0:
                self.network_fail += 1
                self.all_network_fail += 1
            elif 200 <= status < 300 and code == 0:
                self.success += 1
                self.all_success += 1
                if borrow_id is not None:
                    self.cycle_borrow_ids.append(borrow_id)
            elif 200 <= status < 300:
                self.business_fail += 1
                self.all_business_fail += 1
            else:
                self.http_fail += 1
                self.all_http_fail += 1
            if code != 0 and len(self.errors) < self.args.max_error_samples:
                self.errors.append({
                    "httpStatus": status,
                    "code": code,
                    "message": message,
                })
            if message and len(self.sample_messages) < 8 and message not in self.sample_messages:
                self.sample_messages.append(message)
            if message and len(self.all_messages) < 20 and message not in self.all_messages:
                self.all_messages.append(message)

    def snapshot_cycle(self):
        with self.lock:
            return {
                "success": self.success,
                "businessFail": self.business_fail,
                "httpFail": self.http_fail,
                "networkFail": self.network_fail,
                "latencies": list(self.cycle_latencies),
                "statusBreakdown": dict(self.status_counts),
                "messages": list(self.sample_messages),
            }

    def take_cycle_borrow_ids(self):
        with self.lock:
            ids = list(self.cycle_borrow_ids)
            self.cycle_borrow_ids = []
            return ids

    # ---------------------------------------------------------------- 单请求
    def borrow_once(self, reader_id, thread_index):
        if self.args.fixed_idempotency_key:
            idem_key = self.args.fixed_idempotency_key
        else:
            idem_key = "bench-{}-{}-{}".format(self.args.label, thread_index,
                                               uuid.uuid4().hex[:16])
        body = json.dumps({"readerId": reader_id, "bookId": self.args.book_id})
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Idempotency-Key": idem_key,
            "X-Trace-Id": "bench-{}".format(uuid.uuid4().hex[:16]),
            # 压测流量标记：便于在后端日志里把压测请求与真实请求分开
            "X-Bench-Label": self.args.label,
        }
        started = time.perf_counter()
        status, payload, raw, error = http_request(
            self.target, "POST", "/api/borrows", body=body, headers=headers,
            timeout=self.args.timeout)
        latency_ms = (time.perf_counter() - started) * 1000.0

        if error is not None:
            self.record(latency_ms, 0, None, error, None)
            return

        code = None
        message = ""
        borrow_id = None
        if isinstance(payload, dict):
            raw_code = payload.get("code")
            code = raw_code if isinstance(raw_code, int) else None
            message = payload.get("msg") or ""
            data = payload.get("data")
            if isinstance(data, dict):
                borrow_id = data.get("borrowId") or data.get("id")
        if code is None and not isinstance(payload, dict):
            message = (raw or "")[:200]
        self.record(latency_ms, status, code, message, borrow_id)

    def worker(self, thread_index: int, request_count: int):
        for _ in range(request_count):
            reader_id = self.pool.next_reader()
            self.borrow_once(reader_id, thread_index)

    # ---------------------------------------------------------------- 状态读取
    def read_stock_cached(self):
        """
        公开接口 GET /api/books/{id}。
        注意：它读的是多级缓存，库存刚变化时可能返回旧值（实测踩过，见模块 docstring）。
        只用于"读者视角"的交叉验证，不作为判据来源。
        """
        status, payload, raw, error = http_request(
            self.target, "GET", "/api/books/{}".format(self.args.book_id),
            headers={"Accept": "application/json"}, timeout=self.args.timeout)
        if error is not None or status != 200 or not isinstance(payload, dict):
            return {"ok": False, "note": "读取书目(缓存)失败: {} / http={}".format(error, status)}
        data = payload.get("data") or {}
        return {
            "ok": True,
            "title": data.get("title"),
            "totalCopies": data.get("totalCopies"),
            "available": extract_available(data),
        }

    def read_stock_authoritative(self):
        """
        内部接口 GET /internal/agent/tools/books/{id}/stock（HMAC-SHA256 签名）。
        走 StockService.view -> BookStockMapper，直读数据库，不经过书目缓存，是权威值。

        canonical = timestamp + "\\n" + METHOD + "\\n" + pathWithQuery + "\\n" + bodyUtf8
        sign      = lowercaseHex(HMAC_SHA256(secret, canonical))
        （与后端 InternalSignFilter 完全一致；见蓝图 §8.2）
        """
        path = "/internal/agent/tools/books/{}/stock".format(self.args.book_id)
        timestamp = str(int(time.time()))
        canonical = "{}\nGET\n{}\n".format(timestamp, path)
        signature = hmac.new(self.args.internal_sign_secret.encode("utf-8"),
                             canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {
            "Accept": "application/json",
            "X-Internal-Timestamp": timestamp,
            "X-Internal-Sign": signature,
        }
        status, payload, raw, error = http_request(
            self.target, "GET", path, headers=headers, timeout=self.args.timeout)
        if error is not None:
            return {"ok": False, "note": "读取库存(内部接口)失败: {}".format(error)}
        if status != 200 or not isinstance(payload, dict):
            return {"ok": False,
                    "note": "读取库存(内部接口)失败: http={} body={}".format(
                        status, (raw or "")[:160])}
        data = payload.get("data") or {}
        available = data.get("availableCopies")
        if not isinstance(available, int):
            available = data.get("available")
        total = data.get("totalCopies")
        if not isinstance(total, int):
            total = data.get("total")
        return {
            "ok": isinstance(available, int),
            "title": data.get("title"),
            "totalCopies": total,
            "available": available,
            "queueLen": data.get("queueLen"),
            "shelfLocation": data.get("shelfLocation"),
        }

    def read_stock(self):
        """
        同时读权威值与缓存值，返回合并结果。
        判据（扣减守恒、库存是否复原、是否超卖）一律用权威值；
        缓存值只用于记录"读者看到的可能是旧值"这一事实。
        """
        cached = self.read_stock_cached()
        authoritative = None
        if self.args.stock_source in ("auto", "internal"):
            authoritative = self.read_stock_authoritative()

        prefer_authoritative = bool(authoritative and authoritative.get("ok"))
        if prefer_authoritative:
            source = "internal-signed"
            available = authoritative.get("available")
            total = authoritative.get("totalCopies")
            title = authoritative.get("title") or cached.get("title")
        elif cached.get("ok"):
            source = "book-detail-cache"
            available = cached.get("available")
            total = cached.get("totalCopies")
            title = cached.get("title")
        else:
            source = "none"
            available = None
            total = None
            title = None

        cache_diverged = None
        if prefer_authoritative and cached.get("ok"):
            cache_diverged = (authoritative.get("available") != cached.get("available"))

        note = ""
        if not prefer_authoritative:
            note = ("权威库存接口不可用，已退化为读缓存值；此时扣减守恒判据不可信"
                    "（缓存可能返回旧值）。内部接口失败原因：{}".format(
                        (authoritative or {}).get("note")))

        return {
            "ok": isinstance(available, int),
            "source": source,
            "available": available,
            "totalCopies": total,
            "title": title,
            "availableAuthoritative": (authoritative or {}).get("available"),
            "availableCached": cached.get("available"),
            "cacheDiverged": cache_diverged,
            "note": note,
        }

    def return_borrow(self, borrow_id):
        """归还一笔借阅：这是"每轮重置库存"的实现手段（纯公开 API，不需要改后端）。"""
        status, payload, raw, error = http_request(
            self.target, "POST", "/api/borrows/{}/return".format(borrow_id),
            headers={"Accept": "application/json"}, timeout=self.args.timeout)
        if error is not None:
            return False, "{}: {}".format(type(error).__name__, error)
        if status != 200:
            message = ""
            if isinstance(payload, dict):
                message = payload.get("msg") or ""
            return False, "http={} msg={}".format(status, message or (raw or "")[:120])
        if isinstance(payload, dict) and payload.get("code") not in (0, None):
            return False, "code={} msg={}".format(payload.get("code"), payload.get("msg"))
        return True, None

    def return_all(self, borrow_ids):
        ok_count = 0
        failures = []
        for borrow_id in borrow_ids:
            ok, reason = self.return_borrow(borrow_id)
            if ok:
                ok_count += 1
            elif len(failures) < 10:
                failures.append({"borrowId": borrow_id, "reason": reason})
        return ok_count, failures

    # ---------------------------------------------------------------- 主流程
    def run(self):
        args = self.args
        self.per_thread_requests = max(1, args.requests // args.threads)
        total_per_cycle = self.per_thread_requests * args.threads
        total_planned = total_per_cycle * args.cycles

        probe = self.read_stock()
        print("[load_borrow] 目标      : {}:{}{}".format(
            self.target["host"], self.target["port"], self.target["prefix"] or "/"))
        print("[load_borrow] 书目 id   : {}".format(args.book_id))
        print("[load_borrow] 轮次配置  : {} 轮 x ({} 线程 x {} 次) = {} 次请求".format(
            args.cycles, args.threads, self.per_thread_requests, total_planned))
        print("[load_borrow] 读者池    : {} 人 {} ，分配模式={}".format(
            len(self.reader_ids), self.reader_ids, args.reader_mode))
        print("[load_borrow] 每轮归还  : {}".format(
            "是（每轮借出后归还，库存回到初始值）" if args.return_each_cycle
            else "否（各轮共享库存，指标不可跨轮比较）"))
        print("[load_borrow] 库存来源  : {}（权威=内部签名接口，缓存=GET /api/books）".format(
            probe.get("source")))
        print("[load_borrow] 标签      : {}".format(args.label))
        if probe.get("ok"):
            print("[load_borrow] 压测前库存: 权威 {} / 缓存 {}（总复本 {}）{}".format(
                probe.get("availableAuthoritative"), probe.get("availableCached"),
                probe.get("totalCopies"),
                "，两者不一致（缓存偏旧）" if probe.get("cacheDiverged") else ""))
        else:
            print("[load_borrow] 压测前检查: {}".format(probe.get("note")))

        cycles = []
        started_all = time.perf_counter()
        for cycle in range(1, args.cycles + 1):
            before = self.read_stock()
            before_available = before.get("available")
            self.reset_cycle_counters()

            print("")
            print("[load_borrow] ---------- cycle {}/{} 开始（权威可借 {}，来源 {}） ----------".format(
                cycle, args.cycles, before_available, before.get("source")))

            started = time.perf_counter()
            threads = []
            for index in range(args.threads):
                thread = threading.Thread(target=self.worker,
                                          args=(index, self.per_thread_requests),
                                          name="bench-c{}-t{}".format(cycle, index))
                thread.daemon = True
                threads.append(thread)
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            elapsed = time.perf_counter() - started

            snapshot = self.snapshot_cycle()
            borrow_ids = self.take_cycle_borrow_ids()
            after = self.read_stock()
            after_available = after.get("available")

            # 每轮期望成功数 = min(该轮开始时可借复本, 本轮实际请求数, 干净读者数)
            # 为什么不是直接用"可借复本数"：
            #   如果书的复本多于本轮请求数（例如 12 复本只压 8 次），
            #   成功数当然是 8 而不是 12 —— 那不是误拒，是我们没借那么多。
            #   同理 --reader-mode distinct 下，读者数不够时也借不满。
            # 三者取最小才是"本可以成功多少次"的正确上界，拿它当期望才有意义。
            expected = args.expect_success
            if expected is None:
                bounds = [self.per_thread_requests * args.threads]
                if isinstance(before_available, int):
                    bounds.append(before_available)
                if args.reader_mode == "distinct":
                    bounds.append(len(self.reader_ids))
                expected = min(bounds)

            # 守恒判据：成功 n 次就必须恰好扣掉 n 册
            deduction_ok = None
            if isinstance(before_available, int) and isinstance(after_available, int):
                deduction_ok = (after_available == before_available - snapshot["success"])

            returned = 0
            return_failures = []
            after_return = None
            if args.return_each_cycle:
                returned, return_failures = self.return_all(borrow_ids)
                after_return = self.read_stock()

            cycle_latencies = sorted(snapshot["latencies"])
            cycle_record = {
                "cycle": cycle,
                "bookId": args.book_id,
                "title": before.get("title"),
                "totalCopies": before.get("totalCopies"),
                "stockSource": before.get("source"),
                "availableBefore": before_available,
                "availableAfterBorrow": after_available,
                "availableAfterReturn": (after_return or {}).get("available"),
                "availableCachedAfterBorrow": after.get("availableCached"),
                "availableCachedAfterReturn": (after_return or {}).get("availableCached"),
                "cacheDivergedAfterBorrow": after.get("cacheDiverged"),
                "cacheDivergedAfterReturn": (after_return or {}).get("cacheDiverged"),
                "expectedSuccess": expected,
                "successCount": snapshot["success"],
                "successMatchesExpected": (None if expected is None
                                           else snapshot["success"] == expected),
                "deductionConsistent": deduction_ok,
                "oversold": (None if not isinstance(after_available, int)
                             else after_available < 0),
                "borrowedTotal": len(borrow_ids),
                "borrowIds": borrow_ids,
                "returnedCount": returned,
                "returnFailures": return_failures,
                "stockRestored": (None if not args.return_each_cycle
                                  else (after_return or {}).get("available") == before_available),
                "durationSeconds": round(elapsed, 4),
                "requestsTotal": len(cycle_latencies),
                "requestsPerSecond": round(len(cycle_latencies) / elapsed, 2)
                                     if elapsed > 0 else 0.0,
                "successPerSecond": round(snapshot["success"] / elapsed, 2)
                                    if elapsed > 0 else 0.0,
                "latencyMs": {
                    "min": round(cycle_latencies[0], 3) if cycle_latencies else 0.0,
                    "p50": round(percentile(cycle_latencies, 50), 3),
                    "p90": round(percentile(cycle_latencies, 90), 3),
                    "p99": round(percentile(cycle_latencies, 99), 3),
                    "max": round(cycle_latencies[-1], 3) if cycle_latencies else 0.0,
                },
                "businessFailCount": snapshot["businessFail"],
                "httpFailCount": snapshot["httpFail"],
                "networkFailCount": snapshot["networkFail"],
                "statusBreakdown": snapshot["statusBreakdown"],
                "messageSamples": snapshot["messages"],
                "readersUsedInCycle": self.pool.used.copy(),
                "readerReuseRounds": self.pool.reuse_rounds,
            }
            cycles.append(cycle_record)
            print("[load_borrow] cycle {} 结果：成功 {}/{} 期望，扣减守恒={}，超卖={}，"
                  "归还 {} 笔，权威库存 {} -> {} -> {}，缓存库存 {}（{}）".format(
                      cycle, snapshot["success"], expected, deduction_ok,
                      cycle_record["oversold"], returned, before_available,
                      after_available, cycle_record["availableAfterReturn"],
                      cycle_record["availableCachedAfterBorrow"],
                      "与权威不一致" if cycle_record["cacheDivergedAfterBorrow"]
                      else "与权威一致"))

            if not args.return_each_cycle and cycle < args.cycles:
                print("[load_borrow] 警告：未开启 --return-each-cycle，"
                      "下一轮将从已扣减的库存继续，跨轮指标不可比较")

        elapsed_all = time.perf_counter() - started_all
        return self.build_report(elapsed_all, total_planned, cycles, probe)

    # ---------------------------------------------------------------- 汇总
    def build_report(self, elapsed, total_planned, cycles, probe):
        args = self.args
        latencies = sorted(self.latencies)
        total = len(latencies)
        qps = (total / elapsed) if elapsed > 0 else 0.0
        failed = self.all_business_fail + self.all_http_fail + self.all_network_fail
        error_rate = (failed / total) if total else 0.0

        expected_total = 0
        actual_total = 0
        for record in cycles:
            if isinstance(record["expectedSuccess"], int):
                expected_total += record["expectedSuccess"]
                actual_total += record["successCount"]
        cycles_with_expectation = sum(1 for r in cycles if isinstance(r["expectedSuccess"], int))

        oversold_cycles = [r["cycle"] for r in cycles if r["oversold"] is True]
        unknown_oversell = [r["cycle"] for r in cycles if r["oversold"] is None]
        deduction_bad = [r["cycle"] for r in cycles if r["deductionConsistent"] is False]
        restore_bad = [r["cycle"] for r in cycles if r["stockRestored"] is False]
        expectation_bad = [r["cycle"] for r in cycles if r["successMatchesExpected"] is False]
        cache_stale_cycles = [r["cycle"] for r in cycles if r["cacheDivergedAfterBorrow"] is True]
        stock_sources = sorted({r["stockSource"] for r in cycles if r.get("stockSource")})

        return {
            "label": args.label,
            "kind": "borrow",
            "timestamp": now_iso(),
            "measurementDesign": {
                "cycles": args.cycles,
                "returnEachCycle": args.return_each_cycle,
                "stockResetPerCycle": args.return_each_cycle,
                "readerMode": args.reader_mode,
                "readerPoolSize": len(self.reader_ids),
                "readerIds": self.reader_ids,
                "stockSource": (stock_sources[0] if len(stock_sources) == 1
                                else (stock_sources or ["none"])),
                "stockSourceExplain": (
                    "internal-signed = GET /internal/agent/tools/books/{id}/stock（HMAC 签名，"
                    "直读数据库，权威）；book-detail-cache = GET /api/books/{id}（走多级缓存，"
                    "库存刚变化时可能返回旧值，只作读者视角参考）。"),
                "cacheStaleCycles": cache_stale_cycles,
                "cacheStaleExplain": (
                    "这些轮次里 GET /api/books/{id} 返回的 availableCopies 与权威库存不一致"
                    "（缓存未随库存变化失效）。实测确认：后端唯一的 StockChangeListener 实现是"
                    "InvertedIndexSearchEngine，没有任何代码失效书目详情缓存。"
                    "影响的是【读者看到的可借数】而非扣减正确性。"),
                "perCycleExplain": (
                    "每轮借出后把本轮所有 successful borrow 归还（POST /api/borrows/{id}/return），"
                    "使 availableCopies 回到本轮初始值；因此各轮独立、可比。"
                    if args.return_each_cycle else
                    "未开启逐轮归还：各轮共享同一份库存，后面的轮次可能因缺货而失败，"
                    "跨轮指标不可比较，只能当混合负载参考。"),
                "expectationExplain": (
                    "每轮期望成功数 = min(该轮开始时的可借复本数, 本轮请求数, 干净读者数)，"
                    "也就是'本轮本可以成功多少次'。干净读者池 + 每读者只借 1 次时，"
                    "只要书的复本数 <= 请求数，成功数就应当恰好等于复本数（把书借空）："
                    "少了=误拒，多了=超卖。同时用守恒判据"
                    "（扣减后可借 == 扣减前可借 - 成功数）兜底。"),
            },
            "target": {
                "url": args.url,
                "host": self.target["host"],
                "port": self.target["port"],
            },
            "parameters": {
                "bookId": args.book_id,
                "threads": args.threads,
                "requestsPerCycle": args.requests,
                "requestsPerThreadPerCycle": self.per_thread_requests,
                "requestsPerCycleActual": (cycles[0]["requestsTotal"] if cycles else 0),
                "requestsTotalPlanned": total_planned,
                "cycles": args.cycles,
                "readerIds": self.reader_ids,
                "readerMode": args.reader_mode,
                "returnEachCycle": args.return_each_cycle,
                "timeoutSeconds": args.timeout,
                "fixedIdempotencyKey": args.fixed_idempotency_key,
            },
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "processor": platform.processor(),
            },
            "result": {
                "durationSeconds": round(elapsed, 4),
                "requestsTotal": total,
                "qps": round(qps, 2),
                "qpsNote": ("QPS = 总请求数/总耗时（含业务失败请求）。样本 {} 次时抖动较大，"
                            "且每请求新建连接，只能作为【下界】参考".format(total)),
                "latencyMs": {
                    "min": round(latencies[0], 3) if latencies else 0.0,
                    "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
                    "p50": round(percentile(latencies, 50), 3),
                    "p90": round(percentile(latencies, 90), 3),
                    "p99": round(percentile(latencies, 99), 3),
                    "max": round(latencies[-1], 3) if latencies else 0.0,
                    "stdev": round(statistics.pstdev(latencies), 3) if len(latencies) > 1 else 0.0,
                    "note": "跨全部 cycle 汇总的客户端耗时（每请求新建 TCP 连接）",
                },
                "successCount": self.all_success,
                "businessFailCount": self.all_business_fail,
                "httpFailCount": self.all_http_fail,
                "networkFailCount": self.all_network_fail,
                "errorRate": round(error_rate, 6),
                "errorSamples": self.errors,
                "messageSamples": self.all_messages,
                "statusBreakdown": self.all_status_counts,
                "aggregationNote": ("以上是全部 cycle 的累计值；逐轮数字见 cycles[]。"
                                    "successCount 是【成功请求数】，多轮模式下会大于复本数"
                                    "（每轮都能把同一批复本借满并归还）。"),
            },
            "expectation": {
                "expectedTotal": expected_total,
                "actualTotal": actual_total,
                "cyclesWithExpectation": cycles_with_expectation,
                "matched": (expected_total == actual_total) if cycles_with_expectation else None,
                "perCycleMatched": [r["successMatchesExpected"] for r in cycles],
                "mismatchedCycles": expectation_bad,
            },
            "correctness": {
                "oversoldCycles": oversold_cycles,
                "oversellUnknownCycles": unknown_oversell,
                "deductionInconsistentCycles": deduction_bad,
                "stockNotRestoredCycles": restore_bad,
                "cacheStaleCycles": cache_stale_cycles,
                "passed": (not oversold_cycles and not deduction_bad and not restore_bad),
            },
            "cycles": cycles,
            "stockBefore": probe,
            "stockAfter": cycles[-1]["availableAfterReturn"] if cycles else None,
            "oversellCheck": {
                "bookId": args.book_id,
                "oversold": any(r["oversold"] is True for r in cycles) if cycles else None,
                "oversoldCycles": oversold_cycles,
                "afterAvailable": cycles[-1]["availableAfterBorrow"] if cycles else None,
                "afterReturnAvailable": cycles[-1]["availableAfterReturn"] if cycles else None,
                "passed": bool(cycles) and not oversold_cycles and not unknown_oversell,
                "note": ("所有轮次 availableCopies 均 >= 0，未超卖"
                         if cycles and not oversold_cycles and not unknown_oversell
                         else ("存在超卖轮次：{}".format(oversold_cycles) if oversold_cycles
                               else "无法判定（读取书目失败）")),
            },
        }


# =============================================================================
# CLI
# =============================================================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="load_borrow.py",
        description="SmartLib 借阅接口并发压测（纯标准库）+ 逐轮库存重置 + 超卖检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  # 单轮干净测量：8 个干净读者并发抢 8 复本，期望恰好 8 成功\n"
            "  python scripts/bench/load_borrow.py --url http://localhost:8080 \\\n"
            "      --book-id 14 --threads 8 --requests 8 --reader-mode distinct \\\n"
            "      --reader-ids 1,3,4,6,7,8,9,10 --label optimistic-db\n"
            "  # 多轮：每轮借出后归还，库存回到初始，样本量更大\n"
            "  python scripts/bench/load_borrow.py --url http://localhost:8080 \\\n"
            "      --book-id 24 --threads 8 --requests 8 --cycles 5 --return-each-cycle \\\n"
            "      --reader-mode distinct --reader-ids 1,3,4,6,7,8,9,10 --label redis-lua\n"
        ),
    )
    parser.add_argument("--url", required=True,
                        help="后端基地址，如 http://localhost:8080（不要带 /api 后缀）")
    parser.add_argument("--book-id", type=int, required=True, help="抢哪本书（book.id）")
    parser.add_argument("--threads", type=int, default=50, help="并发线程数（默认 50）")
    parser.add_argument("--requests", type=int, default=500,
                        help="【每轮】请求数上限（默认 500）；总请求数 = requests x cycles")
    parser.add_argument("--cycles", type=int, default=1,
                        help="轮次（默认 1）。配 --return-each-cycle 时每轮都从同一初始库存开始")
    parser.add_argument("--return-each-cycle", action="store_true",
                        help="每轮结束后归还本轮所有借出（纯公开 API 实现库存重置）。"
                             "强烈建议开启：不开的话各轮共享库存，指标不可比较")
    parser.add_argument("--expect-success", type=int, default=None,
                        help="每轮期望成功数；缺省 = 该轮开始时的 availableCopies")
    parser.add_argument("--reader-mode", choices=["round-robin", "distinct"], default="round-robin",
                        help="读者分配：round-robin 会重复用同一读者（旧行为）；"
                             "distinct 每个读者只用一次，是'成功数==复本数'判据的前提")
    parser.add_argument("--reader-id", type=int, default=None,
                        help="单个读者 id。会覆盖 --reader-ids 的默认值")
    parser.add_argument("--reader-ids", default="1-30",
                        help="读者池，支持 '1' / '1,2,3' / '1-30'（默认 1-30）。"
                             "注意：必须先用 GET /api/readers/{id}/credit 过滤掉"
                             "信用分不足/有逾期/有欠费/已冻结的读者，否则会引入固定失败")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径（默认不写文件）")
    parser.add_argument("--label", default="default", help="本次压测标签，写进结果文件（默认 default）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数（默认 30）")
    parser.add_argument("--fixed-idempotency-key", default=None,
                        help="所有请求复用同一个幂等键：用于验证幂等（重复只生效一次），"
                             "压测并发扣减时不要开")
    parser.add_argument("--max-error-samples", type=int, default=20,
                        help="结果文件里最多保留多少条错误样本（默认 20）")
    parser.add_argument("--allow-unreset-stock", action="store_true",
                        help="显式承认'各轮不重置库存'（避免不小心跑出不可比较的数据）")
    parser.add_argument("--stock-source", choices=["auto", "internal", "book-detail"],
                        default="auto",
                        help="库存读取来源：auto（默认，优先内部签名接口，失败退化缓存）/"
                             "internal（只用内部签名接口）/ book-detail（只用缓存，仅读者视角）")
    parser.add_argument("--internal-sign-secret", default="dev-internal-secret-please-change",
                        help="内部接口 HMAC 密钥，必须与后端 smartlib.internal.sign-secret 一致"
                             "（默认值即后端默认值；生产一般由 SMARTLIB_INTERNAL_SIGN 覆盖）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.threads <= 0:
        print("[load_borrow] 错误：--threads 必须为正整数", file=sys.stderr)
        return 2
    if args.requests <= 0:
        print("[load_borrow] 错误：--requests 必须为正整数", file=sys.stderr)
        return 2
    if args.cycles <= 0:
        print("[load_borrow] 错误：--cycles 必须为正整数", file=sys.stderr)
        return 2
    if args.book_id <= 0:
        print("[load_borrow] 错误：--book-id 必须为正整数", file=sys.stderr)
        return 2
    if args.reader_id is not None:
        args.reader_ids = str(args.reader_id)

    # 多轮 + 不重置库存 = 必然产出不可比较的数据，直接拦住（要跑必须显式承认）
    if args.cycles > 1 and not args.return_each_cycle and not args.allow_unreset_stock:
        print("[load_borrow] 错误：--cycles > 1 时必须开 --return-each-cycle，"
              "否则后续轮次会因库存被前一轮借空而失败，指标不可比较。"
              "确实想要混合负载请显式加 --allow-unreset-stock。", file=sys.stderr)
        return 2

    try:
        benchmark = BorrowBenchmark(args)
    except ValueError as error:
        print("[load_borrow] 错误：{}".format(error), file=sys.stderr)
        return 2

    report = benchmark.run()

    result = report["result"]
    oversell = report["oversellCheck"]
    correctness = report["correctness"]
    expectation = report["expectation"]
    print("")
    print("[load_borrow] ================= 结果（label={}） =================".format(args.label))
    print("[load_borrow] 轮次/请求   : {} 轮 / {} 次".format(args.cycles, result["requestsTotal"]))
    print("[load_borrow] 库存来源     : {}".format(
        report["measurementDesign"]["stockSource"]))
    print("[load_borrow] 实际耗时     : {:.3f}s".format(result["durationSeconds"]))
    print("[load_borrow] QPS          : {:.2f}（样本 {} 次，小样本仅供参考）".format(
        result["qps"], result["requestsTotal"]))
    print("[load_borrow] 延迟(ms)     : P50={} P90={} P99={} max={}".format(
        result["latencyMs"]["p50"], result["latencyMs"]["p90"],
        result["latencyMs"]["p99"], result["latencyMs"]["max"]))
    print("[load_borrow] 成功借出     : {}".format(result["successCount"]))
    print("[load_borrow] 错误率       : {:.4%}（业务失败 {} / HTTP 失败 {} / 网络失败 {}）".format(
        result["errorRate"], result["businessFailCount"],
        result["httpFailCount"], result["networkFailCount"]))
    print("[load_borrow] 期望 vs 实际 : {}/{} 成功，逐轮匹配 {}".format(
        expectation["actualTotal"], expectation["expectedTotal"], expectation["perCycleMatched"]))
    print("[load_borrow] 扣减守恒违规 : {}".format(
        correctness["deductionInconsistentCycles"] or "无"))
    print("[load_borrow] 库存未复原轮 : {}".format(
        correctness["stockNotRestoredCycles"] or "无"))
    print("[load_borrow] 缓存偏旧轮次 : {}".format(
        correctness["cacheStaleCycles"] or "无（缓存与权威一致）"))
    print("[load_borrow] 超卖检查     : {} —— {}".format(
        "通过" if oversell["passed"] else "未通过", oversell["note"]))
    if result["messageSamples"]:
        print("[load_borrow] 消息样本     : {}".format(result["messageSamples"]))

    if args.out:
        out_path = os.path.abspath(args.out)
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print("[load_borrow] 结果已写入   : {}".format(out_path))

    # 退出码：超卖/扣减不守恒/库存没复原视为硬失败；期望不匹配单独一个码
    if oversell.get("oversold") is True or correctness["deductionInconsistentCycles"]:
        return 3
    if result["requestsTotal"] > 0 and result["successCount"] == 0:
        return 4
    if expectation["matched"] is False or correctness["stockNotRestoredCycles"]:
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
