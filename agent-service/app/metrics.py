"""进程内指标收集（对齐蓝图 §7 的 Agent SLO 指标）。

为什么自己写而不用 prometheus_client：
- 蓝图 §7 的指标只需要"能读出来做验收"，进程内计数器足够；
- 零额外依赖意味着评测脚本、pytest 都能直接复用同一份指标口径，
  不会出现"测试里算一套、监控里算另一套"。

时间窗口：统一用**滚动窗口**（默认 5 分钟）而不是全量累计，
因为 SLO 关心的是"最近的用户请求体验怎么样"，全量平均会被历史数据稀释。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

#: 与蓝图 §7 对齐的目标值，/admin/metrics 里直接给出达标判断
SLO_TARGETS: dict[str, float] = {
    "intent_accuracy": 0.92,
    "tool_arg_accuracy": 0.95,
    "e2e_success_rate": 0.88,
    "first_token_latency_ms": 1500.0,
    "tool_evidence_ratio": 1.0,
}


class Metrics:
    """线程安全的滚动窗口指标。

    只保存最近 window_seconds 内的样本，避免长跑进程内存无界增长。
    """

    def __init__(self, window_seconds: float = 300.0) -> None:
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        # (timestamp, value_ms, labels)
        self._latencies: deque[tuple[float, float, dict[str, Any]]] = deque()
        self._first_token: deque[tuple[float, float]] = deque()
        self._counters: dict[str, int] = {}
        self._steps: deque[tuple[float, int]] = deque()
        self._evidence: deque[tuple[float, bool]] = deque()
        self._started_at = time.time()
        self._total_requests = 0

    # ---------------- 写入 ----------------
    def _gc(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._latencies and self._latencies[0][0] < cutoff:
            self._latencies.popleft()
        while self._first_token and self._first_token[0][0] < cutoff:
            self._first_token.popleft()
        while self._steps and self._steps[0][0] < cutoff:
            self._steps.popleft()
        while self._evidence and self._evidence[0][0] < cutoff:
            self._evidence.popleft()

    def incr(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def observe_chat(
        self,
        *,
        latency_ms: float,
        steps: int,
        evidence: bool,
        first_token_ms: float | None = None,
        labels: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._gc(now)
            self._total_requests += 1
            self._latencies.append((now, latency_ms, labels or {}))
            self._steps.append((now, steps))
            self._evidence.append((now, evidence))
            if first_token_ms is not None:
                self._first_token.append((now, first_token_ms))

    def observe_llm(self, latency_ms: float) -> None:
        with self._lock:
            self._gc(time.time())
            self.incr("llm_calls")
            key = "llm_latency_ms_sum"
            self._counters[key] = self._counters.get(key, 0) + int(latency_ms)

    # ---------------- 读取 ----------------
    @staticmethod
    def _pct(sorted_values: list[float], q: float) -> float:
        if not sorted_values:
            return 0.0
        if len(sorted_values) == 1:
            return float(sorted_values[0])
        # 线性插值分位数，和 numpy.percentile 默认口径一致，便于和压测脚本对齐
        idx = (len(sorted_values) - 1) * q
        lo = int(idx)
        hi = min(lo + 1, len(sorted_values) - 1)
        frac = idx - lo
        return float(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._gc(now)
            lat = sorted(v for _, v, _ in self._latencies)
            ft = sorted(v for _, v in self._first_token)
            steps = [s for _, s in self._steps]
            evidence = [e for _, e in self._evidence]
            counters = dict(self._counters)
            total_requests = self._total_requests
            latency_samples = [
                {"latency_ms": v, "labels": labels} for _, v, labels in self._latencies
            ]

        evidence_ratio = (sum(1 for e in evidence if e) / len(evidence)) if evidence else 0.0
        return {
            "window_seconds": self.window_seconds,
            "uptime_seconds": round(now - self._started_at, 1),
            "total_requests": total_requests,
            "window_requests": len(lat),
            "latency_ms": {
                "avg": round(sum(lat) / len(lat), 1) if lat else 0.0,
                "p50": round(self._pct(lat, 0.5), 1),
                "p95": round(self._pct(lat, 0.95), 1),
                "p99": round(self._pct(lat, 0.99), 1),
                "max": round(max(lat), 1) if lat else 0.0,
            },
            "first_token_ms": {
                "p50": round(self._pct(ft, 0.5), 1),
                "p95": round(self._pct(ft, 0.95), 1),
            },
            "avg_steps": round(sum(steps) / len(steps), 2) if steps else 0.0,
            "tool_evidence_ratio": round(evidence_ratio, 4),
            "counters": counters,
            "slo": {
                "first_token_latency_ms_target": SLO_TARGETS["first_token_latency_ms"],
                "tool_evidence_ratio_target": SLO_TARGETS["tool_evidence_ratio"],
                "tool_evidence_ratio_ok": evidence_ratio >= SLO_TARGETS["tool_evidence_ratio"]
                if evidence
                else None,
            },
        }

    def reset(self) -> None:
        with self._lock:
            self._latencies.clear()
            self._first_token.clear()
            self._steps.clear()
            self._evidence.clear()
            self._counters.clear()
            self._total_requests = 0


#: 进程级单例。FastAPI 的 admin 接口和 agent 运行时共用这一个实例，
#: 这样 /admin/metrics 读到的一定是真实请求产生的数据。
METRICS = Metrics()
