package com.smartlib.common;

/**
 * 错误码。
 *
 * <p>分段规则（前端与 Agent 依赖分段做统一处理，禁止随意占用区段）：
 * <ul>
 *   <li>1xxx 参数与协议错误（HTTP 400）</li>
 *   <li>2xxx 业务规则错误（HTTP 404/403/409）</li>
 *   <li>3xxx 库存相关错误（HTTP 409/500）</li>
 *   <li>4xxx 认证与权限（HTTP 401/403）</li>
 *   <li>5xxx 系统内部错误（HTTP 500）</li>
 *   <li>9xxx 外部依赖与限流降级（HTTP 409/503）</li>
 * </ul>
 */
public enum ErrorCode {

    // ---------------- 1xxx 参数与协议 ----------------
    PARAM_INVALID(1001, "参数不合法", 400),
    IDEM_KEY_REQUIRED(1002, "缺少幂等键 Idempotency-Key", 400),
    PAGE_OUT_OF_RANGE(1003, "分页参数超出允许范围", 400),
    UNSUPPORTED_STRATEGY(1004, "不支持的库存扣减策略", 400),

    // ---------------- 2xxx 业务规则 ----------------
    READER_NOT_FOUND(2001, "读者不存在", 404),
    BOOK_NOT_FOUND(2002, "图书不存在", 404),
    BORROW_RECORD_NOT_FOUND(2003, "借阅记录不存在", 404),
    BOOK_ALREADY_BORROWED(2004, "同一本书不能重复借阅", 409),
    BORROW_LIMIT_EXCEEDED(2005, "已达到可借册数上限", 409),
    READER_HAS_OVERDUE(2006, "存在逾期未还图书，请先归还", 409),
    CREDIT_TOO_LOW(2007, "信用分不足，暂不可借阅", 403),
    READER_FROZEN(2008, "读者状态异常，暂不可借阅", 403),
    RENEW_LIMIT_EXCEEDED(2009, "续借次数已用尽", 409),
    RENEW_HAS_RESERVATION(2010, "该书已被预约，不可续借", 409),
    BORROW_STATUS_INVALID(2011, "当前借阅状态不允许该操作", 409),
    RESERVATION_DUPLICATED(2012, "已预约该书，请勿重复提交", 409),
    RESERVATION_NOT_FOUND(2013, "预约记录不存在", 404),
    FINE_UNPAID(2014, "存在未缴清罚金，请先缴纳", 403),

    // ---------------- 3xxx 库存 ----------------
    STOCK_NOT_ENOUGH(3001, "库存不足", 409),
    STOCK_LEDGER_CONFLICT(3002, "库存流水冲突（bizNo 重复）", 500),
    STOCK_STRATEGY_UNKNOWN(3003, "库存策略未注册", 500),
    STOCK_DATA_INCONSISTENT(3004, "库存数据不一致", 500),

    // ---------------- 4xxx 认证与权限 ----------------
    UNAUTHORIZED(4001, "未认证", 401),
    FORBIDDEN(4002, "无权访问", 403),
    INTERNAL_SIGN_INVALID(4003, "内部接口签名校验失败", 401),

    // ---------------- 5xxx 系统 ----------------
    SYSTEM_ERROR(5001, "系统繁忙，请稍后重试", 500),
    DB_ERROR(5002, "数据库访问异常", 500),

    // ---------------- 9xxx 外部依赖与降级 ----------------
    EXTERNAL_DEPENDENCY_ERROR(9001, "外部依赖异常", 503),
    REDIS_UNAVAILABLE(9002, "缓存服务不可用", 503),
    SEARCH_ENGINE_ERROR(9003, "检索引擎异常", 503),
    IDEM_IN_PROGRESS(9004, "相同请求正在处理中，请勿重复提交", 409),
    RATE_LIMITED(9005, "请求过于频繁，请稍后重试", 429),
    CIRCUIT_OPEN(9006, "依赖已熔断，已返回降级结果", 200);

    private final int code;
    private final String message;
    private final int httpStatus;

    ErrorCode(int code, String message, int httpStatus) {
        this.code = code;
        this.message = message;
        this.httpStatus = httpStatus;
    }

    public int getCode() {
        return code;
    }

    public String getMessage() {
        return message;
    }

    public int getHttpStatus() {
        return httpStatus;
    }
}
