#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SmartLib 检索接口压测脚本（纯标准库，零依赖）

测什么
    并发 GET /api/books?query=...，对比 §6.2 五种检索方案（Q1~Q5）的表现：
      * QPS / P50 / P90 / P99 / 最大耗时
      * 错误率（HTTP 状态码 + 业务 code）
      * --recall-check 打开时：对人工标注集计算召回率
        （命中期望 id 的比例，判据来自蓝图 §7"检索召回率 >= 95%"）

关于 --engine 参数
    后端的检索引擎可以通过管理接口运行期切换（PUT /api/admin/search-engine?name=like），
    前提是 smartlib.admin.enabled=true。本脚本有两种用法：
      a) 自己切换：加 --switch-engine，脚本会先 PUT 切换（并可 --reindex），再压测；
      b) 外部切换：不加 --switch-engine，--engine 就只是结果文件里的标签，
         由 run_all.ps1 之类的外层编排负责切换。
    无论哪种方式，结果文件里都会记录 requestedEngine / observedEngine，
    避免出现"标签写着 Q3、实际跑的是 Q1"这种编造数据的情况。

用法
    # Q1 基线：先切到 like 再压测
    python scripts/bench/search_bench.py --url http://localhost:8080 \
        --engine like --switch-engine --threads 20 --requests 400 --label Q1-like

    # Q3 自研倒排：先切到 inverted-index，重建索引（大数据量导入后必须重建），再压测
    python scripts/bench/search_bench.py --url http://localhost:8080 \
        --engine inverted-index --switch-engine --reindex \
        --threads 20 --requests 400 --label Q3-inverted

    # 带召回率评估（同时把数据集当作查询集）
    python scripts/bench/search_bench.py --url http://localhost:8080 \
        --engine inverted-index --switch-engine \
        --queries-file scripts/bench/recall_dataset.json \
        --recall-check scripts/bench/recall_dataset.json --label Q3-recall
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


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def percentile(sorted_values, pct: float) -> float:
    """线性插值分位数（与 load_borrow.py 口径一致，便于横向比较）。"""
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
    except Exception as error:
        return 0, None, "", "{}: {}".format(type(error).__name__, error)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# 查询集合
# =============================================================================
# 内置默认查询集：取自 data.sql 里真实存在的书目/作者，保证"一定召回得到东西"，
# 否则压测数字会被"空结果快速返回"污染（空结果比有结果的查询快得多）。
DEFAULT_QUERIES = [
    "Java", "Java 并发", "虚拟机", "计算机网络", "自顶向下",
    "数据库 事务", "算法导论", "设计模式", "重构", "计算机系统",
    "Elasticsearch", "倒排索引", "Redis", "缓存", "MySQL 索引",
    "慢查询", "Spring", "IoC", "数学分析", "高等代数",
    "概率论 数理统计", "离散数学", "经济学原理", "管理学", "红楼梦",
    "平凡的世界", "活着", "万历十五年", "西方哲学史", "新概念英语",
]


def load_queries(args) -> list:
    """
    查询集来源优先级：
      1) --queries-file 里带 "query" 字段的 JSON（可直接复用召回标注集）
      2) --queries-file 是纯文本，每行一条（# 开头为注释）
      3) --queries 逗号分隔字符串
      4) 内置 DEFAULT_QUERIES
    """
    if args.queries_file:
        path = os.path.abspath(args.queries_file)
        if not os.path.isfile(path):
            raise ValueError("--queries-file 不存在：{}".format(path))
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        stripped = text.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            dataset = json.loads(text)
            if isinstance(dataset, dict):
                dataset = dataset.get("queries") or dataset.get("dataset") or []
            queries = [item.get("query") for item in dataset
                       if isinstance(item, dict) and item.get("query")]
            if not queries:
                raise ValueError("--queries-file 是 JSON 但没有找到任何 query 字段")
            return queries
        queries = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append(line)
        if not queries:
            raise ValueError("--queries-file 里没有有效查询行")
        return queries

    if args.queries:
        queries = [item.strip() for item in args.queries.split(",") if item.strip()]
        if not queries:
            raise ValueError("--queries 解析后为空")
        return queries

    return list(DEFAULT_QUERIES)


# =============================================================================
# 压测主体
# =============================================================================
class SearchBenchmark:
    def __init__(self, args):
        self.args = args
        self.target = parse_base_url(args.url)
        self.queries = load_queries(args)
        self.lock = threading.Lock()
        self.latencies = []
        self.status_counts = {}
        self.success = 0
        self.empty_result = 0
        self.business_fail = 0
        self.http_fail = 0
        self.network_fail = 0
        self.errors = []
        self.message_samples = []
        self.took_ms_samples = []
        self.per_thread_requests = 0
        # 引擎切换/重建索引的结果，会写进报告，便于证伪"标签与实际不一致"
        self.engineSwitch = {"requested": args.engine, "attempted": False}
        self.observedEngines = {}

    # ------------------------------------------------------------ 引擎切换
    def admin_call(self, method: str, path: str):
        """调用管理接口，返回 (ok, status, payload_or_text)。"""
        status, payload, raw, error = http_request(
            self.target, method, path, headers={"Accept": "application/json"},
            timeout=self.args.timeout)
        if error is not None:
            return False, 0, error
        return (200 <= status < 300), status, (payload if payload is not None else raw)

    def switch_engine(self):
        """
        先 PUT /api/admin/search-engine?name=<engine> 再压测。
        这个接口由 AdminController 提供，需要 smartlib.admin.enabled=true；
        未开启时返回 404，此时给出明确提示而不是静默失败（否则数据会张冠李戴）。
        """
        info = {"requested": self.args.engine, "attempted": True}
        ok, status, payload = self.admin_call(
            "PUT", "/api/admin/search-engine?name=" + self.args.engine)
        info["switchHttpStatus"] = status
        info["switchOk"] = ok
        if not ok:
            info["switchNote"] = ("切换失败（HTTP {}）。请确认后端已配置 "
                                  "smartlib.admin.enabled=true，且该引擎已注册。"
                                  "本次压测的 --engine 将只是标签。").format(status)
            print("[search_bench] 警告：{}".format(info["switchNote"]))
            self.engineSwitch = info
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            info["previous"] = data.get("previous")
            info["current"] = data.get("current")
            info["available"] = data.get("available")
        print("[search_bench] 引擎已切换：{} -> {}".format(
            info.get("previous"), info.get("current")))

        if self.args.reindex:
            # 大数据量导入后必须重建倒排索引（否则索引为空，召回率会假性为 0）
            started = time.perf_counter()
            ok_r, status_r, payload_r = self.admin_call("POST", "/api/admin/search/reindex")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            info["reindexOk"] = ok_r
            info["reindexHttpStatus"] = status_r
            info["reindexMillis"] = round(elapsed_ms, 2)
            if ok_r and isinstance(payload_r, dict) and isinstance(payload_r.get("data"), dict):
                info["reindexStats"] = payload_r["data"]
                print("[search_bench] 索引已重建：{}（{:.0f} ms）".format(
                    payload_r["data"].get("summary"), elapsed_ms))
            else:
                info["reindexNote"] = "重建索引失败（HTTP {}）：{}".format(status_r, payload_r)
                print("[search_bench] 警告：{}".format(info["reindexNote"]))
        self.engineSwitch = info

    def query_current_engine(self):
        """读一次 /api/admin/search-engine，拿服务端"实际"当前引擎名。"""
        ok, status, payload = self.admin_call("GET", "/api/admin/search-engine")
        if ok and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"].get("current")
        return None

    def search_once(self, query: str, extra_params=None):
        params = {
            # 后端 GET /api/books 的检索参数名是 query
            "query": query,
            "page": 1,
            "size": self.args.size,
            "sort": self.args.sort,
            "availableOnly": "true" if self.args.available_only else "false",
        }
        if extra_params:
            params.update(extra_params)
        path = "/api/books?" + urlencode(params)
        headers = {
            "Accept": "application/json",
            "X-Trace-Id": "bench-search-{}".format(uuid.uuid4().hex[:16]),
            "X-Bench-Label": self.args.label,
        }
        started = time.perf_counter()
        status, payload, raw, error = http_request(
            self.target, "GET", path, headers=headers, timeout=self.args.timeout)
        latency_ms = (time.perf_counter() - started) * 1000.0
        return status, payload, raw, error, latency_ms

    def record(self, latency_ms, status, code, message, total_hits, took_ms):
        with self.lock:
            self.latencies.append(latency_ms)
            code_key = "{} (code={})".format(status if status else "network-error",
                                             code if code is not None else "-")
            self.status_counts[code_key] = self.status_counts.get(code_key, 0) + 1
            if status == 0:
                self.network_fail += 1
            elif 200 <= status < 300 and code == 0:
                self.success += 1
                if total_hits == 0:
                    self.empty_result += 1
            elif 200 <= status < 300:
                self.business_fail += 1
            else:
                self.http_fail += 1
            if code != 0 and len(self.errors) < self.args.max_error_samples:
                self.errors.append({"httpStatus": status, "code": code, "message": message})
            if message and len(self.message_samples) < 8 and message not in self.message_samples:
                self.message_samples.append(message)
            if isinstance(took_ms, (int, float)):
                self.took_ms_samples.append(float(took_ms))

    def worker(self, thread_index: int, request_count: int):
        total_queries = len(self.queries)
        for local_index in range(request_count):
            query = self.queries[(thread_index + local_index) % total_queries]
            status, payload, raw, error, latency_ms = self.search_once(query)
            if error is not None:
                self.record(latency_ms, 0, None, error, None, None)
                continue
            code = None
            message = ""
            total_hits = None
            took_ms = None
            if isinstance(payload, dict):
                raw_code = payload.get("code")
                code = raw_code if isinstance(raw_code, int) else None
                message = payload.get("msg") or ""
                data = payload.get("data")
                if isinstance(data, dict):
                    if isinstance(data.get("total"), int):
                        total_hits = data.get("total")
                    if isinstance(data.get("tookMs"), (int, float)):
                        took_ms = data.get("tookMs")
            if code is None and not isinstance(payload, dict):
                message = (raw or "")[:200]
            self.record(latency_ms, status, code, message, total_hits, took_ms)

    def run(self):
        args = self.args
        self.per_thread_requests = max(1, args.requests // args.threads)

        if args.switch_engine:
            self.switch_engine()
        else:
            # 不切换时也读一次当前引擎，写进报告，避免"标签与事实不符"
            self.engineSwitch = {
                "requested": args.engine,
                "attempted": False,
                "current": self.query_current_engine(),
                "note": "未使用 --switch-engine：--engine 只是标签，服务端引擎由配置/外部编排决定",
            }

        print("[search_bench] 目标      : {}:{}{}".format(
            self.target["host"], self.target["port"], self.target["prefix"] or "/"))
        print("[search_bench] 引擎标签  : {}（服务端实际：{}）".format(
            args.engine, self.engineSwitch.get("current")))
        print("[search_bench] 查询集    : {} 条".format(len(self.queries)))
        print("[search_bench] 线程/请求 : {} 线程 x {} 次 = {} 次".format(
            args.threads, self.per_thread_requests,
            self.per_thread_requests * args.threads))

        threads = []
        started = time.perf_counter()
        for index in range(args.threads):
            thread = threading.Thread(target=self.worker,
                                      args=(index, self.per_thread_requests),
                                      name="bench-search-{}".format(index))
            thread.daemon = True
            threads.append(thread)
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - started

        report = self.build_report(elapsed)
        # 压测后再读一次引擎名：如果这期间被外部切走了，报告里能看出来
        report["engineObservedAfter"] = self.query_current_engine()
        if args.recall_check:
            report["recall"] = self.evaluate_recall(args.recall_check)
        return report

    def build_report(self, elapsed):
        args = self.args
        latencies = sorted(self.latencies)
        total = len(latencies)
        qps = (total / elapsed) if elapsed > 0 else 0.0
        failed = self.business_fail + self.http_fail + self.network_fail
        error_rate = (failed / total) if total else 0.0
        server_took = sorted(self.took_ms_samples)
        return {
            "label": args.label,
            "kind": "search",
            "timestamp": now_iso(),
            "engine": args.engine,
            "requestedEngine": args.engine,
            "engineSwitch": self.engineSwitch,
            "engineNote": ("--engine 是结果文件里的期望标签；engineSwitch.current 是服务端"
                           "切换后的实际引擎名。两者不一致时以 engineSwitch / "
                           "engineObservedAfter 为准（这才是真实跑了什么）。"),
            "target": {
                "url": args.url,
                "host": self.target["host"],
                "port": self.target["port"],
            },
            "parameters": {
                "engine": args.engine,
                "threads": args.threads,
                "requestsRequested": args.requests,
                "requestsPerThread": self.per_thread_requests,
                "requestsActual": total,
                "size": args.size,
                "sort": args.sort,
                "availableOnly": args.available_only,
                "queryCount": len(self.queries),
                "timeoutSeconds": args.timeout,
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
                "serverTookMs": {
                    "samples": len(server_took),
                    "p50": round(percentile(server_took, 50), 3) if server_took else None,
                    "p99": round(percentile(server_took, 99), 3) if server_took else None,
                    "note": "后端 data.tookMs（服务端自报耗时），与客户端口径对照用",
                },
                "successCount": self.success,
                "emptyResultCount": self.empty_result,
                "businessFailCount": self.business_fail,
                "httpFailCount": self.http_fail,
                "networkFailCount": self.network_fail,
                "errorRate": round(error_rate, 6),
                "errorSamples": self.errors,
                "messageSamples": self.message_samples,
                "statusBreakdown": self.status_counts,
            },
        }

    # ------------------------------------------------------------------ 召回率
    def evaluate_recall(self, dataset_path: str):
        """
        召回率计算口径（必须写清楚，否则数字没意义）：
          * 数据集每条形如 {"query": "...", "expected_book_ids": [1, 2]}
          * 对每条 query 发一次 GET /api/books?query=...&size=<--recall-size>
          * 取返回的 data.list 里的 id 集合（兼容 id / bookId 字段）
          * recall_i = |命中 ∩ 期望| / |期望|
          * 平均召回率 = mean(recall_i)
          * hitAt1 = 期望 id 出现在第一条结果里的条数 / 总条数
          * 【被跳过的条目】缺少 query 或 expected_book_ids 为空的条目会记 skipped，
            **不计入分母**（否则会人为拉低或拉高召回率）。所以想加"负样本"时不要把
            期望写成 []，而应该单独统计空结果率（result.emptyResultCount）。
          * 【局限】期望集是人工标注的"至少应该被召回"的小集合，
            不是完整的相关文档全集，所以这是"可用性召回"而不是学术意义的 recall。
        """
        path = os.path.abspath(dataset_path)
        if not os.path.isfile(path):
            raise ValueError("--recall-check 数据集不存在：{}".format(path))
        with open(path, "r", encoding="utf-8") as handle:
            dataset = json.load(handle)
        if isinstance(dataset, dict):
            dataset = dataset.get("queries") or dataset.get("dataset") or []
        if not isinstance(dataset, list) or not dataset:
            raise ValueError("召回数据集必须是形如 "
                             "[{\"query\":\"...\",\"expected_book_ids\":[1,2]}] 的非空数组")

        details = []
        total_recall = 0.0
        evaluated = 0
        skipped = 0
        hit_at_1 = 0
        for item in dataset:
            if not isinstance(item, dict):
                skipped += 1
                continue
            query = item.get("query")
            expected = item.get("expected_book_ids") or []
            if not query or not expected:
                details.append({"query": query, "skipped": True,
                                "reason": "缺少 query 或 expected_book_ids（空标注不计入召回率分母）"})
                skipped += 1
                continue
            status, payload, raw, error, latency_ms = self.search_once(
                query, extra_params={"size": self.args.recall_size})
            got_ids = []
            if error is None and status == 200 and isinstance(payload, dict):
                data = payload.get("data") or {}
                items = data.get("list") or data.get("items") or []
                for row in items:
                    if not isinstance(row, dict):
                        continue
                    value = row.get("id")
                    if value is None:
                        value = row.get("bookId")
                    if isinstance(value, int):
                        got_ids.append(value)
            hit = [bid for bid in expected if bid in got_ids]
            recall = len(hit) / float(len(expected))
            total_recall += recall
            evaluated += 1
            if got_ids and got_ids[0] in expected:
                hit_at_1 += 1
            details.append({
                "query": query,
                "expected": expected,
                "returnedTop": got_ids[:self.args.recall_size],
                "hit": hit,
                "recall": round(recall, 4),
                "hitAt1": bool(got_ids and got_ids[0] in expected),
                "httpStatus": status,
                "error": error,
                "latencyMs": round(latency_ms, 3),
            })

        average = (total_recall / evaluated) if evaluated else 0.0
        return {
            "datasetPath": path,
            "datasetSize": len(dataset),
            "evaluatedQueries": evaluated,
            "skippedEntries": skipped,
            "recallSize": self.args.recall_size,
            "averageRecall": round(average, 4),
            "averageRecallPercent": round(average * 100, 2),
            "hitAt1Count": hit_at_1,
            "hitAt1Rate": round(hit_at_1 / float(evaluated), 4) if evaluated else 0.0,
            "currentEngineLabel": self.args.engine,
            "engineSwitch": self.engineSwitch,
            "note": ("引擎以 engineSwitch.current / engineObservedAfter 为准；"
                     "召回率口径见 search_bench.py evaluate_recall 的 docstring"),
            "details": details,
        }


# =============================================================================
# CLI
# =============================================================================
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="search_bench.py",
        description="SmartLib 检索接口并发压测 + 召回率评估（纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/bench/search_bench.py --url http://localhost:8080 \\\n"
            "      --engine like --threads 20 --requests 400 --label Q1-like\n"
            "  python scripts/bench/search_bench.py --url http://localhost:8080 \\\n"
            "      --engine inverted-index --queries-file scripts/bench/recall_dataset.json \\\n"
            "      --recall-check scripts/bench/recall_dataset.json --label Q3-recall\n"
        ),
    )
    parser.add_argument("--url", required=True,
                        help="后端基地址，如 http://localhost:8080（不要带 /api 后缀）")
    parser.add_argument("--engine", default="unknown",
                        help="引擎名（like / inverted-index / elasticsearch）。"
                             "配 --switch-engine 时会真的 PUT 切换，否则只是结果标签")
    parser.add_argument("--switch-engine", action="store_true",
                        help="压测前调用 PUT /api/admin/search-engine?name=<engine> 真正切换引擎"
                             "（需要后端 smartlib.admin.enabled=true）")
    parser.add_argument("--reindex", action="store_true",
                        help="切换后调用 POST /api/admin/search/reindex 重建倒排索引"
                             "（批量导入书目后必须重建，否则索引为空会让召回率假性为 0）")
    parser.add_argument("--queries", default=None,
                        help="逗号分隔的查询词；缺省时用内置 30 条中文查询")
    parser.add_argument("--queries-file", default=None,
                        help="查询文件：纯文本每行一条，或 JSON 数组（含 query 字段）")
    parser.add_argument("--threads", type=int, default=20, help="并发线程数（默认 20）")
    parser.add_argument("--requests", type=int, default=400, help="总请求数上限（默认 400）")
    parser.add_argument("--size", type=int, default=20, help="每页条数（默认 20，对应蓝图 TopK 20）")
    parser.add_argument("--sort", default="relevance", help="排序方式（默认 relevance）")
    parser.add_argument("--available-only", action="store_true",
                        help="只检索可借书目（availableOnly=true）")
    parser.add_argument("--recall-check", default=None,
                        help="召回率评估数据集路径，如 scripts/bench/recall_dataset.json")
    parser.add_argument("--recall-size", type=int, default=20,
                        help="召回评估时每条 query 取多少条结果（默认 20）")
    parser.add_argument("--out", default=None, help="结果 JSON 输出路径（默认不写文件）")
    parser.add_argument("--label", default="default", help="本次压测标签（默认 default）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数（默认 30）")
    parser.add_argument("--max-error-samples", type=int, default=20,
                        help="结果文件里最多保留多少条错误样本（默认 20）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.threads <= 0:
        print("[search_bench] 错误：--threads 必须为正整数", file=sys.stderr)
        return 2
    if args.requests <= 0 and not args.recall_check:
        print("[search_bench] 错误：--requests 必须为正整数", file=sys.stderr)
        return 2
    if args.size <= 0 or args.recall_size <= 0:
        print("[search_bench] 错误：--size / --recall-size 必须为正整数", file=sys.stderr)
        return 2

    try:
        benchmark = SearchBenchmark(args)
    except ValueError as error:
        print("[search_bench] 错误：{}".format(error), file=sys.stderr)
        return 2

    report = benchmark.run()
    result = report["result"]

    print("")
    print("[search_bench] ================ 结果（label={}） ================".format(args.label))
    print("[search_bench] 实际耗时     : {:.3f}s".format(result["durationSeconds"]))
    print("[search_bench] 请求总数     : {}".format(result["requestsTotal"]))
    print("[search_bench] QPS          : {:.2f}".format(result["qps"]))
    print("[search_bench] 延迟(ms)     : P50={} P90={} P99={} max={}".format(
        result["latencyMs"]["p50"], result["latencyMs"]["p90"],
        result["latencyMs"]["p99"], result["latencyMs"]["max"]))
    print("[search_bench] 成功/空结果  : {} / {}".format(
        result["successCount"], result["emptyResultCount"]))
    print("[search_bench] 错误率       : {:.4%}（业务失败 {} / HTTP 失败 {} / 网络失败 {}）".format(
        result["errorRate"], result["businessFailCount"],
        result["httpFailCount"], result["networkFailCount"]))
    print("[search_bench] 状态分布     : {}".format(result["statusBreakdown"]))
    if report["result"]["serverTookMs"]["samples"]:
        print("[search_bench] 服务端耗时   : P50={}ms P99={}ms（后端 data.tookMs）".format(
            report["result"]["serverTookMs"]["p50"], report["result"]["serverTookMs"]["p99"]))
    if "recall" in report:
        recall = report["recall"]
        print("[search_bench] 召回率       : {:.2f}%（{} 条查询，命中期望 id 的比例）".format(
            recall["averageRecallPercent"], recall["evaluatedQueries"]))
        print("[search_bench] Top1 命中率  : {:.2%}".format(recall["hitAt1Rate"]))

    if args.out:
        out_path = os.path.abspath(args.out)
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print("[search_bench] 结果已写入   : {}".format(out_path))

    if result["requestsTotal"] > 0 and result["successCount"] == 0:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
