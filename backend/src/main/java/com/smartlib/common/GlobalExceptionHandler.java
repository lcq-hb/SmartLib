package com.smartlib.common;

import java.util.stream.Collectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.BindException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.MissingRequestHeaderException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.servlet.NoHandlerFoundException;

import jakarta.validation.ConstraintViolationException;

/**
 * 全局异常处理。
 *
 * <p>设计原则：
 * <ol>
 *   <li>可预期失败（BizException）按错误码映射 HTTP 状态，不打 ERROR 日志（避免告警噪音）；</li>
 *   <li>参数校验失败统一收敛为 1001，并把字段级错误拼进 msg，方便前端与 Agent 直接展示；</li>
 *   <li>未知异常一律 5xxx 并打 ERROR（带堆栈），这是需要告警的部分。</li>
 * </ol>
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(BizException.class)
    public ResponseEntity<Result<Void>> handleBiz(BizException e) {
        ErrorCode ec = e.getErrorCode();
        // 业务失败属于正常流量的一部分，用 WARN 记录关键信息即可
        log.warn("业务异常 code={} msg={}", ec.getCode(), e.getMessage());
        return ResponseEntity.status(ec.getHttpStatus())
                .body(Result.fail(ec.getCode(), e.getMessage()));
    }

    @ExceptionHandler({MethodArgumentNotValidException.class, BindException.class})
    public ResponseEntity<Result<Void>> handleValidation(BindException e) {
        String detail = e.getBindingResult().getFieldErrors().stream()
                .map(fe -> fe.getField() + ": " + fe.getDefaultMessage())
                .collect(Collectors.joining("; "));
        log.warn("参数校验失败 {}", detail);
        return ResponseEntity.status(400)
                .body(Result.fail(ErrorCode.PARAM_INVALID, ErrorCode.PARAM_INVALID.getMessage() + " - " + detail));
    }

    @ExceptionHandler(ConstraintViolationException.class)
    public ResponseEntity<Result<Void>> handleConstraint(ConstraintViolationException e) {
        String detail = e.getConstraintViolations().stream()
                .map(v -> v.getPropertyPath() + ": " + v.getMessage())
                .collect(Collectors.joining("; "));
        log.warn("参数约束失败 {}", detail);
        return ResponseEntity.status(400)
                .body(Result.fail(ErrorCode.PARAM_INVALID, ErrorCode.PARAM_INVALID.getMessage() + " - " + detail));
    }

    @ExceptionHandler(MissingRequestHeaderException.class)
    public ResponseEntity<Result<Void>> handleMissingHeader(MissingRequestHeaderException e) {
        log.warn("缺少请求头 {}", e.getHeaderName());
        return ResponseEntity.status(400)
                .body(Result.fail(ErrorCode.PARAM_INVALID, "缺少请求头 " + e.getHeaderName()));
    }

    @ExceptionHandler(NoHandlerFoundException.class)
    public ResponseEntity<Result<Void>> handleNotFound(NoHandlerFoundException e) {
        return ResponseEntity.status(404).body(Result.fail(ErrorCode.PARAM_INVALID, "接口不存在: " + e.getRequestURL()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Result<Void>> handleUnknown(Exception e) {
        // 未知异常必须带堆栈打 ERROR：这才是需要告警与排查的部分
        log.error("未捕获异常 type={}", e.getClass().getName(), e);
        return ResponseEntity.status(500).body(Result.fail(ErrorCode.SYSTEM_ERROR));
    }
}
