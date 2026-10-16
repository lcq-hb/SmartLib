package com.smartlib.web;

import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;
import com.smartlib.common.Result;
import com.smartlib.domain.dto.Dtos.BorrowResult;
import com.smartlib.service.BorrowService;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;

/**
 * 借阅接口。
 */
@RestController
@RequestMapping("/api/borrows")
@Tag(name = "借阅", description = "借书、还书、续借")
public class BorrowController {

    private final BorrowService borrowService;

    public BorrowController(BorrowService borrowService) {
        this.borrowService = borrowService;
    }

    /** 借书请求体。 */
    public record BorrowRequest(
            @NotNull(message = "readerId 不能为空") @Positive(message = "readerId 必须为正数") Long readerId,
            @NotNull(message = "bookId 不能为空") @Positive(message = "bookId 必须为正数") Long bookId) {
    }

    /**
     * 借书。
     *
     * <p>{@code Idempotency-Key} 用 required=false 声明，是为了能在缺失时返回业务错误码 1002
     * 而不是框架的通用参数错误 1001 —— 前者能让前端明确知道"要生成一个幂等键再重试"。
     */
    @PostMapping
    @Operation(summary = "借书", description = "必须携带 Idempotency-Key 头；同一 key 重复提交只会借出一本")
    public Result<BorrowResult> borrow(@Valid @RequestBody BorrowRequest request,
                                       @RequestHeader(value = "Idempotency-Key", required = false) String idemKey) {
        if (idemKey == null || idemKey.isBlank()) {
            throw new BizException(ErrorCode.IDEM_KEY_REQUIRED);
        }
        BorrowResult result = borrowService.borrow(request.readerId(), request.bookId(), idemKey.trim());
        if (!result.ok()) {
            // 业务失败必须体现为 HTTP 状态码 + 错误码，而不是"200 但 ok=false"：
            // 前者能被网关、监控、重试策略、压测脚本统一识别；后者需要每个调用方自己判断，
            // 而实际上总有调用方会漏判，把失败当成成功。
            throw new BizException(ErrorCode.STOCK_NOT_ENOUGH, result.reason());
        }
        return Result.ok(result);
    }

    @PostMapping("/{id}/return")
    @Operation(summary = "还书")
    public Result<BorrowResult> returnBook(@PathVariable long id) {
        return Result.ok(borrowService.returnBook(id));
    }

    @PostMapping("/{id}/renew")
    @Operation(summary = "续借", description = "有预约排队时不允许续借；续借次数与单册最长借期受 rule_config 约束")
    public Result<BorrowResult> renew(@PathVariable long id,
                                      @RequestBody(required = false) RenewRequest body) {
        if (body == null || body.readerId() == null) {
            throw new BizException(ErrorCode.PARAM_INVALID, "readerId 不能为空");
        }
        return Result.ok(borrowService.renew(body.readerId(), id));
    }

    public record RenewRequest(@NotNull Long readerId) {
    }
}
