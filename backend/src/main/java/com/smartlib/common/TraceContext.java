package com.smartlib.common;

import org.slf4j.MDC;

/**
 * 一次请求的 traceId 上下文。
 *
 * <p>traceId 在入口 {@link TraceIdFilter} 中生成（或从上游 X-Trace-Id 继承），
 * 随后：写入 MDC（进日志）→ 放入响应头 → 通过 HTTP 头透传给 Agent 服务，
 * 从而让一次 Agent 对话跨越 Python 与 Java 两个进程后仍能串起完整链路。
 *
 * <p>使用 ThreadLocal，必须在 finally 中 {@link #clear()}，否则线程池复用会造成串号。
 */
public final class TraceContext {

    public static final String TRACE_ID_HEADER = "X-Trace-Id";
    private static final String MDC_KEY = "traceId";
    private static final ThreadLocal<String> HOLDER = new ThreadLocal<>();

    private TraceContext() {
    }

    public static void set(String traceId) {
        HOLDER.set(traceId);
        MDC.put(MDC_KEY, traceId);
    }

    /** 返回当前 traceId；不存在时返回空串而不是 null，避免日志出现 "null"。 */
    public static String get() {
        String traceId = HOLDER.get();
        return traceId == null ? "" : traceId;
    }

    public static void clear() {
        HOLDER.remove();
        MDC.remove(MDC_KEY);
    }
}
