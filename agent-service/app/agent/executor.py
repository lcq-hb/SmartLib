"""工具执行器：把"工具名 + 原始参数"变成"可信的 ToolResult"。

它承担的是 **Agent 与工具之间的防腐层**：
- 白名单校验（未注册的工具名 → 拒绝）
- Pydantic 强校验（参数类型错 → 变成"请补充信息"的对话，而不是 500）
- 单步超时（蓝图 §6.4；工具内部已有 3s HTTP 超时，这里再加一层兜底，
  防止工具体实现里自己写出慢逻辑拖死整个会话）
- 循环检测（同一工具+同一参数重复调用 → 中断，避免 LLM 绕圈烧 token）
- 可观测（步数、耗时、错误码都进 ToolCallTrace，供 /admin/traces 与评测使用）

**不做重试**：重试在 java_client 里做（那里知道哪些请求幂等安全）。
executor 再重试会放大成 3×3 次调用，对后端是攻击而不是韧性。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from pydantic import ValidationError

from app.api.schemas import ToolCallTrace
from app.errors import SmartLibError, friendly_reason
from app.tools import registry
from app.tools.base import ToolContext, ToolError, ToolResult


def args_fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    """参数指纹：用于循环检测与幂等键生成。

    排序 key 后再序列化，保证 {"a":1,"b":2} 与 {"b":2,"a":1} 视为同一个调用。
    """
    try:
        payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(sorted(args.items(), key=lambda kv: kv[0]))
    return f"{tool_name}:{payload}"


class ToolExecutor:
    """执行器。每次对话创建一个实例（持有本次调用的 trace 记录）。"""

    def __init__(self, ctx: ToolContext, *, step_timeout: float | None = None) -> None:
        self.ctx = ctx
        self.step_timeout = step_timeout or ctx.settings.step_timeout_seconds
        self.traces: list[ToolCallTrace] = []
        #: 指纹 → 调用次数，用于循环检测
        self._call_counts: dict[str, int] = {}
        self._step = 0
        self._loop_repeat_limit = ctx.settings.loop_repeat_limit

    # ---------------- 校验 ----------------
    def validate(self, tool_name: str, raw_args: dict[str, Any]) -> tuple[Any, ToolResult | None]:
        """返回 (校验后的参数模型, 失败结果)。二者只有一个非 None。"""
        tool = registry.get(tool_name)
        if tool is None:
            return None, ToolResult(
                ok=False,
                tool=tool_name,
                error_code="TOOL_NOT_FOUND",
                error_message=f"工具 {tool_name} 不在白名单内（可用：{sorted(registry.names())}）",
            )
        try:
            args = tool.args_model.model_validate(raw_args or {})
        except ValidationError as exc:
            # 把 Pydantic 的英文报错压成一句读者能懂的中文，并保留 details 供排查
            details = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            return None, ToolResult(
                ok=False,
                tool=tool_name,
                error_code="TOOL_ARGUMENT_INVALID",
                error_message=details,
            )
        return args, None

    def loop_detected(self, tool_name: str, raw_args: dict[str, Any]) -> bool:
        return self._call_counts.get(args_fingerprint(tool_name, raw_args), 0) >= self._loop_repeat_limit

    # ---------------- 执行 ----------------
    async def execute(
        self, tool_name: str, raw_args: dict[str, Any], *, internal: bool = False
    ) -> ToolResult:
        """执行一个工具。

        internal=True 表示这是**运行时内部补的步骤**（例如为了拿到真实 book_id
        而补的 search_books），不是模型或读者要求的那次调用。它照常执行并计入
        traces，但带 internal 标记，便于上游区分"读者问的工具"与"内部辅助步骤"。
        """
        self._step += 1
        step = self._step
        started = time.perf_counter()

        tool = registry.get(tool_name)
        if tool is None:
            result = ToolResult(
                ok=False,
                tool=tool_name,
                error_code="TOOL_NOT_FOUND",
                error_message=f"工具 {tool_name} 不在白名单内",
            )
            self._record(step, tool_name, raw_args, result, blocked_by="tool_whitelist", internal=internal)
            return result

        fingerprint = args_fingerprint(tool_name, raw_args)
        if self._call_counts.get(fingerprint, 0) >= self._loop_repeat_limit:
            result = ToolResult(
                ok=False,
                tool=tool_name,
                error_code="LOOP_DETECTED",
                error_message=(
                    f"相同调用已重复 {self._call_counts[fingerprint]} 次，判定为循环，已中断"
                ),
            )
            self._record(step, tool_name, raw_args, result, blocked_by="loop_detection", internal=internal)
            return result
        self._call_counts[fingerprint] = self._call_counts.get(fingerprint, 0) + 1

        args, failure = self.validate(tool_name, raw_args)
        if failure is not None:
            failure.latency_ms = (time.perf_counter() - started) * 1000
            self._record(step, tool_name, raw_args, failure, blocked_by="argument_validation", internal=internal)
            return failure

        try:
            # 单步超时兜底：超时后协程被取消，工具内的 httpx 请求也会一并取消
            result = await asyncio.wait_for(tool.handle(args, self.ctx), timeout=self.step_timeout)
        except asyncio.TimeoutError:
            result = ToolResult(
                ok=False,
                tool=tool_name,
                error_code="STEP_TIMEOUT",
                error_message=f"工具 {tool_name} 执行超过 {self.step_timeout}s",
            )
        except ToolError as exc:
            result = ToolResult(
                ok=False,
                tool=tool_name,
                error_code=exc.code,
                error_message=exc.user_message,
            )
        except SmartLibError as exc:
            result = ToolResult(
                ok=False,
                tool=tool_name,
                error_code=exc.code,
                error_message=exc.user_message,
            )
        except Exception as exc:  # noqa: BLE001 - 工具层不许把异常抛给用户
            result = ToolResult(
                ok=False,
                tool=tool_name,
                error_code="TOOL_INTERNAL_ERROR",
                error_message=f"工具执行异常，请稍后再试。（{type(exc).__name__}）",
            )

        result.latency_ms = (time.perf_counter() - started) * 1000
        self._record(step, tool_name, raw_args, result, internal=internal)
        return result

    def _record(
        self,
        step: int,
        tool_name: str,
        raw_args: dict[str, Any],
        result: ToolResult,
        *,
        blocked_by: str | None = None,
        internal: bool = False,
    ) -> None:
        self.traces.append(
            ToolCallTrace(
                step=step,
                tool=tool_name,
                # 落 trace 时把幂等键隐去，避免日志里到处都是可用于重放的 key
                arguments={k: v for k, v in (raw_args or {}).items() if k != "idempotencyKey"},
                ok=result.ok,
                latency_ms=round(result.latency_ms, 1),
                error_code=result.error_code,
                error_message=result.error_message,
                internal=internal,
                summary=result.summary
                or (friendly_reason(result.error_code, result.error_message or "") if not result.ok else ""),
                blocked_by=blocked_by,
            )
        )

    @property
    def steps(self) -> int:
        return self._step

    def has_evidence(self) -> bool:
        """是否拿到了可用于回答事实性问题工具证据。"""
        return any(t.ok for t in self.traces)

    def error_codes(self) -> list[str]:
        return [t.error_code for t in self.traces if t.error_code]

    def last_result_summary(self) -> str:
        for trace in reversed(self.traces):
            if trace.summary:
                return trace.summary
        return ""


__all__ = ["ToolExecutor", "args_fingerprint"]
