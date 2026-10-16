#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SmartLib 借阅接口压测脚本（纯标准库，零依赖）

测什么
    并发 POST /api/borrows，验证 §6.1 六种库存扣减策略在同一套请求下的表现：
      * QPS / P50 / P90 / P99 / 最大耗时
      * 错误率（按 HTTP 状态码与业务 code 分别统计）
      * 成功借出数（code == 0 的请求数）
      * 【超卖检查】压测结束后 GET /api/books/{id}，看 availableCopies 是否 < 0
        —— 蓝图 §7 的硬指标是"超卖数 = 0"，这一项是唯一不能妥协的。

为什么用 http.client + threading 而不是压测框架
    评审机/CI 上没有 wrk/ab/locust；纯标准库能保证脚本在任何 Python 3.7+ 上直接跑。

【诚实边界】这不是专业压测工具：
    * 每个请求新建一条 TCP 连接（最坏情况口径），没有连接池复用；
      真实网关后端的连接是复用的，所以本脚本测出的 QPS 是【下界】。
    * 客户端本身（GIL + Python 线程）也可能成为瓶颈。
    因此结论表述必须是"在 N 线程、每请求新建连接的客户端条件下测得"，不能宣称
    "服务端极限 QPS"。要测服务端上限请用 wrk/JMeter，并对比两者差异。

用法
    python scripts/bench/load_borrow.py --url http://localhost:8080 \
        --book-id 1 --threads 50 --requests 500 --label optimistic-db \
        --out scripts/bench/results/borrow_optimistic-db.json

    # 抢购场景：200 并发抢同一本书（对应 §6.1 验收线"200 线程抢 10 册"）
    python scripts/bench/load_borrow.py --url http://localhost:8081 \
        --book-id 5 --threads 200 --requests 200 --reader-ids 1,2,3,4,5,6,7,8,9,10,11 \
        --label pessimistic-db
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse, urlencode


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


# =============================================================================
# 压测主体
# =============================================================================
class BorrowBenchmark:
    def __init__(self, args):
        self.args = args
        self.target = parse_base_url(args.url)
        self.reader_ids = parse_reader_ids(args.reader_ids)
        self.lock = threading.Lock()
        # 结果收集：所有线程往共享列表里追加，加锁保护
        self.latencies = []          # 每个请求的耗时(ms)
        self.status_counts = {}      # "200 (code=0)" -> 次数
        self.success = 0             # code == 0
        self.business_fail = 0       # HTTP 2xx 但 code != 0
        self.http_fail = 0           # 非 2xx
        self.network_fail = 0        # status == 0
        self.errors = []             # 最多留 --max-error-samples 条样本
        self.sample_messages = []
        self.borrow_ids = []
        self.per_thread_requests = 0

    def record(self, latency_ms, status, code, message, borrow_id):
        with self.lock:
            self.latencies.append(latency_ms)
            code_key = "{} (code={})".format(status if status else "network-error",
                                             code if code is not None else "-")
            self.status_counts[code_key] = self.status_counts.get(code_key, 0) + 1
            if status == 0:
                self.network_fail += 1
            elif 200 <= status < 300 and code == 0:
                self.success += 1
                if borrow_id is not None:
                    self.borrow_ids.append(borrow_id)
            elif 200 <= status < 300:
                self.business_fail += 1
            else:
                self.http_fail += 1
            if code != 0 and len(self.errors) < self.args.max_error_samples:
                self.errors.append({
                    "httpStatus": status,
                    "code": code,
                    "message": message,
                })
            if message and len(self.sample_messages) < 8 and message not in self.sample_messages:
                self.sample_messages.append(message)

    def worker(self, thread_index: int, request_count: int):
        target = self.target
        timeout = self.args.timeout
        book_id = self.args.book_id
        for local_index in range(request_count):
            reader_id = self.reader_ids[(thread_index + local_index) % len(self.reader_ids)]
            # 每次请求唯一幂等键：压测要测的是"并发扣减"，不是幂等表的去重能力。
            # 如果要单独验证幂等（同一 key 重复只生效一次），见 --fixed-idempotency-key。
            if self.args.fixed_idempotency_key:
                idem_key = self.args.fixed_idempotency_key
            else:
                idem_key = "bench-{}-{}-{}".format(self.args.label, thread_index,
                                                   uuid.uuid4().hex[:16])
            body = json.dumps({"readerId": reader_id, "bookId": book_id})
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
                target, "POST", "/api/borrows", body=body, headers=headers, timeout=timeout)
            latency_ms = (time.perf_counter() - started) * 1000.0

            if error is not None:
                self.record(latency_ms, 0, None, error, None)
                continue

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

    def reset_book_state(self):
        """
        压测前查一次库存作为基线，并且把"总复本数"记下来：
        正确性判据是 availableCopies 不能 < 0（§7 超卖数=0）。
        """
        status, payload, raw, error = http_request(
            self.target, "GET", "/api/books/{}".format(self.args.book_id),
            headers={"Accept": "application/json"}, timeout=self.args.timeout)
        if error is not None or status != 200 or not isinstance(payload, dict):
            return {"ok": False, "note": "压测前读取书目失败: {} / http={}".format(error, status)}
        data = payload.get("data") or {}
        return {
            "ok": True,
            "bookId": self.args.book_id,
            "title": data.get("title"),
            "totalCopies": data.get("totalCopies"),
            "beforeAvailable": extract_available(data),
        }

    def after_check(self):
        """
        【超卖检查】压测结束后再读一次，判断可用库存是否变成负数。
        字段名兼容两种写法：availableCopies（camel）与 available_copies（snake）。
        """
        status, payload, raw, error = http_request(
            self.target, "GET", "/api/books/{}".format(self.args.book_id),
            headers={"Accept": "application/json"}, timeout=self.args.timeout)
        if error is not None or status != 200 or not isinstance(payload, dict):
            return {
                "ok": False,
                "note": "压测后读取书目失败: {} / http={}".format(error, status),
                "oversold": None,
            }
        data = payload.get("data") or {}
        available = extract_available(data)
        oversold = (available is not None and available < 0)
        return {
            "ok": True,
            "httpStatus": status,
            "title": data.get("title"),
            "totalCopies": data.get("totalCopies"),
            "afterAvailable": available,
            "oversold": oversold,
            "oversoldUnits": (-available) if oversold else 0,
        }

    def run(self):
        args = self.args
        self.per_thread_requests = max(1, args.requests // args.threads)
        total_planned = self.per_thread_requests * args.threads
        before = self.reset_book_state()

        print("[load_borrow] 目标      : {}:{}{}".format(
            self.target["host"], self.target["port"], self.target["prefix"] or "/"))
        print("[load_borrow] 书目 id   : {}".format(args.book_id))
        print("[load_borrow] 线程/请求 : {} 线程 x {} 次 = {} 次（目标 {}）".format(
            args.threads, self.per_thread_requests, total_planned, args.requests))
        print("[load_borrow] 读者池    : {}".format(self.reader_ids))
        print("[load_borrow] 标签      : {}".format(args.label))
        if before.get("ok"):
            print("[load_borrow] 压测前可借: {}（总复本 {}）".format(
                before.get("beforeAvailable"), before.get("totalCopies")))
        else:
            print("[load_borrow] 压测前检查: {}".format(before.get("note")))

        threads = []
        started = time.perf_counter()
        for index in range(args.threads):
            thread = threading.Thread(target=self.worker,
                                      args=(index, self.per_thread_requests),
                                      name="bench-{}".format(index))
            thread.daemon = True
            threads.append(thread)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - started

        after = self.after_check()
        return self.build_report(elapsed, total_planned, before, after)

    def build_report(self, elapsed, total_planned, before, after):
        args = self.args
        latencies = sorted(self.latencies)
        total = len(latencies)
        qps = (total / elapsed) if elapsed > 0 else 0.0
        failed = self.business_fail + self.http_fail + self.network_fail
        error_rate = (failed / total) if total else 0.0
        report = {
            "label": args.label,
            "kind": "borrow",
            "timestamp": now_iso(),
            "target": {
                "url": args.url,
                "host": self.target["host"],
                "port": self.target["port"],
            },
            "parameters": {
                "bookId": args.book_id,
                "threads": args.threads,
                "requestsRequested": args.requests,
                "requestsPerThread": self.per_thread_requests,
                "requestsActual": total_planned,
                "readerIds": self.reader_ids,
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
                "latencyMs": {
                    "min": round(latencies[0], 3) if latencies else 0.0,
                    "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
                    "p50": round(percentile(latencies, 50), 3),
                    "p90": round(percentile(latencies, 90), 3),
                    "p99": round(percentile(latencies, 99), 3),
                    "max": round(latencies[-1], 3) if latencies else 0.0,
                    "stdev": round(statistics.pstdev(latencies), 3) if len(latencies) > 1 else 0.0,
                },
                "successCount": self.success,
                "businessFailCount": self.business_fail,
                "httpFailCount": self.http_fail,
                "networkFailCount": self.network_fail,
                "errorRate": round(error_rate, 6),
                "errorSamples": self.errors,
                "messageSamples": self.sample_messages,
                "statusBreakdown": self.status_counts,
                "borrowIdSamples": self.borrow_ids[:5],
            },
            "stockBefore": before,
            "stockAfter": after,
            "oversellCheck": {
                "bookId": args.book_id,
                "oversold": after.get("oversold"),
                "afterAvailable": after.get("afterAvailable"),
                "passed": bool(after.get("oversold") is False),
                "note": ("availableCopies >= 0，未超卖"
                         if after.get("oversold") is False
                         else ("availableCopies < 0，发生超卖！" if after.get("oversold")
                               else "无法判定（读取书目失败）")),
            },
        }
        return report


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
    因此并发抢同一本书必须用多个读者，否则测出来的是"重复借阅拦截率"。
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
    return ids


# =============================================================================
# CLI
# =============================================================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="load_borrow.py",
        description="SmartLib 借阅接口并发压测（纯标准库）+ 超卖检查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/bench/load_borrow.py --url http://localhost:8080 \\\n"
            "      --book-id 1 --threads 50 --requests 500 --label optimistic-db\n"
            "  # 200 并发抢同一本书（§6.1 验收场景）\n"
            "  python scripts/bench/load_borrow.py --url http://localhost:8080 \\\n"
            "      --book-id 5 --threads 200 --requests 200 --reader-ids 1-30 --label redis-lua\n"
        ),
    )
    parser.add_argument("--url", required=True,
                        help="后端基地址，如 http://localhost:8080（不要带 /api 后缀）")
    parser.add_argument("--book-id", type=int, required=True, help="抢哪本书（book.id）")
    parser.add_argument("--threads", type=int, default=50, help="并发线程数（默认 50）")
    parser.add_argument("--requests", type=int, default=500, help="总请求数上限（默认 500）")
    parser.add_argument("--reader-id", type=int, default=None,
                        help="单个读者 id。会覆盖 --reader-ids 的默认值")
    parser.add_argument("--reader-ids", default="1-30",
                        help="读者池，支持 '1' / '1,2,3' / '1-30'（默认 1-30）。"
                             "并发抢同一本书必须给多个读者，否则会被"
                             "'同一读者同一书只能持 1 册'的规则拦截")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径（默认不写文件）")
    parser.add_argument("--label", default="default", help="本次压测标签，写进结果文件（默认 default）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数（默认 30）")
    parser.add_argument("--fixed-idempotency-key", default=None,
                        help="所有请求复用同一个幂等键：用于验证幂等（重复只生效一次），"
                             "压测并发扣减时不要开")
    parser.add_argument("--max-error-samples", type=int, default=20,
                        help="结果文件里最多保留多少条错误样本（默认 20）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.threads <= 0:
        print("[load_borrow] 错误：--threads 必须为正整数", file=sys.stderr)
        return 2
    if args.requests <= 0:
        print("[load_borrow] 错误：--requests 必须为正整数", file=sys.stderr)
        return 2
    if args.book_id <= 0:
        print("[load_borrow] 错误：--book-id 必须为正整数", file=sys.stderr)
        return 2
    if args.reader_id is not None:
        args.reader_ids = str(args.reader_id)

    try:
        benchmark = BorrowBenchmark(args)
    except ValueError as error:
        print("[load_borrow] 错误：{}".format(error), file=sys.stderr)
        return 2

    report = benchmark.run()

    result = report["result"]
    oversell = report["oversellCheck"]
    print("")
    print("[load_borrow] ================= 结果（label={}） =================".format(args.label))
    print("[load_borrow] 实际耗时     : {:.3f}s".format(result["durationSeconds"]))
    print("[load_borrow] 请求总数     : {}".format(result["requestsTotal"]))
    print("[load_borrow] QPS          : {:.2f}".format(result["qps"]))
    print("[load_borrow] 延迟(ms)     : P50={} P90={} P99={} max={}".format(
        result["latencyMs"]["p50"], result["latencyMs"]["p90"],
        result["latencyMs"]["p99"], result["latencyMs"]["max"]))
    print("[load_borrow] 成功借出     : {}".format(result["successCount"]))
    print("[load_borrow] 错误率       : {:.4%}（业务失败 {} / HTTP 失败 {} / 网络失败 {}）".format(
        result["errorRate"], result["businessFailCount"],
        result["httpFailCount"], result["networkFailCount"]))
    print("[load_borrow] 状态分布     : {}".format(result["statusBreakdown"]))
    print("[load_borrow] 超卖检查     : {} —— {}".format(
        "通过" if oversell["passed"] else "未通过", oversell["note"]))
    if report["stockBefore"].get("ok") and oversell.get("afterAvailable") is not None:
        print("[load_borrow] 库存变化     : {} -> {}".format(
            report["stockBefore"].get("beforeAvailable"), oversell.get("afterAvailable")))
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

    # 退出码：只有在"发生超卖"或"全部失败"时返回非 0，方便 run_all.ps1 判定
    if oversell.get("oversold") is True:
        return 3
    if result["requestsTotal"] > 0 and result["successCount"] == 0:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
