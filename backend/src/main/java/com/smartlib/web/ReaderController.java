package com.smartlib.web;

import java.util.List;
import java.util.Map;

import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.smartlib.common.Result;
import com.smartlib.domain.dto.Dtos.LoanView;
import com.smartlib.domain.dto.Dtos.RecommendItem;
import com.smartlib.domain.dto.Dtos.ReservationResult;
import com.smartlib.domain.entity.Reservation;
import com.smartlib.service.ReaderService;
import com.smartlib.service.RecommendService;
import com.smartlib.service.ReservationService;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;

/**
 * 读者相关接口：我的借阅、信用分、画像、推荐、预约。
 *
 * <p>合并在一个控制器里是因为它们都挂在 {@code /api/readers/{id}} 这个资源下，
 * 拆成多个控制器反而让前端要维护多份路径前缀。
 */
@RestController
@RequestMapping("/api/readers")
@Tag(name = "读者", description = "我的借阅、信用分、画像、推荐、预约")
public class ReaderController {

    private final ReaderService readerService;
    private final ReservationService reservationService;
    private final RecommendService recommendService;

    public ReaderController(ReaderService readerService,
                            ReservationService reservationService,
                            RecommendService recommendService) {
        this.readerService = readerService;
        this.reservationService = reservationService;
        this.recommendService = recommendService;
    }

    @GetMapping("/{id}/loans")
    @Operation(summary = "我的借阅", description = "一次 join 取回书名与馆藏位置，避免 N+1 查询")
    public Result<List<LoanView>> loans(@PathVariable long id,
                                        @RequestParam(defaultValue = "50") int limit) {
        return Result.ok(readerService.myLoans(id, limit));
    }

    @GetMapping("/{id}/credit")
    @Operation(summary = "信用分与可借权限")
    public Result<Map<String, Object>> credit(@PathVariable long id) {
        return Result.ok(readerService.credit(id));
    }

    @GetMapping("/{id}/profile")
    @Operation(summary = "读者画像", description = "Agent 个性化推荐的上下文；只返回推荐所必需的信息")
    public Result<Map<String, Object>> profile(@PathVariable long id) {
        return Result.ok(readerService.profile(id));
    }

    @GetMapping("/{id}/recommendations")
    @Operation(summary = "个性化推荐", description = "四路召回加权融合，结果带可解释理由")
    public Result<List<RecommendItem>> recommendations(@PathVariable long id,
                                                       @RequestParam(defaultValue = "10") int limit) {
        return Result.ok(recommendService.recommendForReader(id, limit));
    }

    @GetMapping("/{id}/reservations")
    @Operation(summary = "我的预约")
    public Result<List<Reservation>> reservations(@PathVariable long id) {
        return Result.ok(reservationService.listActive(id));
    }

    @PostMapping("/{id}/reservations")
    @Operation(summary = "预约图书", description = "仅在无可借复本时允许预约")
    public Result<ReservationResult> reserve(@PathVariable long id,
                                             @Valid @RequestBody ReserveRequest body) {
        return Result.ok(reservationService.reserve(id, body.bookId()));
    }

    @DeleteMapping("/{id}/reservations/{reservationId}")
    @Operation(summary = "取消预约", description = "取消处于「到馆待取」状态的预约会自动顺延给队列中的下一位")
    public Result<Void> cancel(@PathVariable long id, @PathVariable long reservationId) {
        reservationService.cancel(id, reservationId);
        return Result.ok();
    }

    public record ReserveRequest(
            @NotNull(message = "bookId 不能为空") @Positive(message = "bookId 必须为正数") Long bookId) {
    }
}
