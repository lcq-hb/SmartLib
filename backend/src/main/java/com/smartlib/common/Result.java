package com.smartlib.common;

/**
 * 统一响应体。
 *
 * <p>code == 0 表示成功；非 0 为错误码，分段规则见 {@link ErrorCode}。
 * traceId 由 {@link TraceIdFilter} 在入口生成并透传，Java 与 Python 两侧共用，
 * 便于跨进程排查一次 Agent 对话的完整链路。
 */
public record Result<T>(int code, String msg, T data, String traceId) {

    public static <T> Result<T> ok(T data) {
        return new Result<>(0, "ok", data, TraceContext.get());
    }

    public static Result<Void> ok() {
        return new Result<>(0, "ok", null, TraceContext.get());
    }

    public static <T> Result<T> fail(ErrorCode errorCode) {
        return new Result<>(errorCode.getCode(), errorCode.getMessage(), null, TraceContext.get());
    }

    public static <T> Result<T> fail(ErrorCode errorCode, String msg) {
        return new Result<>(errorCode.getCode(), msg, null, TraceContext.get());
    }

    public static <T> Result<T> fail(int code, String msg) {
        return new Result<>(code, msg, null, TraceContext.get());
    }

    public boolean isSuccess() {
        return code == 0;
    }
}
