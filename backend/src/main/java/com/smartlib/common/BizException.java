package com.smartlib.common;

/**
 * 业务异常。
 *
 * <p>约定：所有可预期的业务失败都抛 BizException 并携带 {@link ErrorCode}，
 * 由 {@link GlobalExceptionHandler} 统一转成标准响应体；
 * 不可预期的异常交给全局兜底（5xxx），绝不吞异常（见编码规范第 4 章）。
 */
public class BizException extends RuntimeException {

    private final ErrorCode errorCode;

    public BizException(ErrorCode errorCode) {
        super(errorCode.getMessage());
        this.errorCode = errorCode;
    }

    public BizException(ErrorCode errorCode, String message) {
        super(message);
        this.errorCode = errorCode;
    }

    public BizException(ErrorCode errorCode, String message, Throwable cause) {
        super(message, cause);
        this.errorCode = errorCode;
    }

    public ErrorCode getErrorCode() {
        return errorCode;
    }

    public static BizException of(ErrorCode errorCode) {
        return new BizException(errorCode);
    }

    public static BizException of(ErrorCode errorCode, String message) {
        return new BizException(errorCode, message);
    }
}
