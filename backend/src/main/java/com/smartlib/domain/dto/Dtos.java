package com.smartlib.domain.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

/**
 * 领域 DTO 集合。
 *
 * <p>全部使用 Java record：不可变、无样板代码、语义清晰。
 * 这也是本项目不用 Lombok 的替代方案（见 ADR-003）。
 */
public final class Dtos {

    private Dtos() {
    }

    /** 书目展示视图：把 book 与 book_stock 合并成前端/Agent 可直接消费的结构。 */
    public record BookView(
            Long id,
            String isbn,
            String title,
            String author,
            String publisher,
            String categoryName,
            Integer publishYear,
            BigDecimal price,
            String tags,
            String summary,
            String shelfLocation,
            int totalCopies,
            int availableCopies) {
        public boolean available() {
            return availableCopies > 0;
        }
    }

    /** 一次检索的结果，附带耗时与所用引擎，便于对比不同方案。 */
    public record SearchResult(
            List<BookView> items,
            long total,
            long tookMs,
            String engine,
            boolean degraded,
            String degradeReason) {
        public static SearchResult empty(String engine, long tookMs) {
            return new SearchResult(List.of(), 0L, tookMs, engine, false, null);
        }
    }

    /** 库存视图，供 Agent 的 get_book_stock 工具使用。 */
    public record StockView(
            Long bookId,
            String title,
            int totalCopies,
            int availableCopies,
            int queueLen,
            String shelfLocation) {
    }

    /**
     * 库存扣减结果。
     *
     * @param strategy      实际执行的策略名，会写入 stock_ledger，便于压测对比各方案
     * @param ledgerWritten 该策略是否已在自己的事务内写入库存流水。
     *                      MQ 串行化策略在独立线程/独立事务中完成"扣减 + 流水"，
     *                      此时 StockService 不能再写一次，否则流水会重复。
     */
    public record StockDeductResult(
            boolean success,
            String strategy,
            String reason,
            int beforeQty,
            int afterQty,
            boolean ledgerWritten) {

        public static StockDeductResult ok(String strategy, int beforeQty, int afterQty) {
            return new StockDeductResult(true, strategy, null, beforeQty, afterQty, false);
        }

        public static StockDeductResult okWithLedger(String strategy, int beforeQty, int afterQty) {
            return new StockDeductResult(true, strategy, null, beforeQty, afterQty, true);
        }

        public static StockDeductResult fail(String strategy, String reason, int beforeQty) {
            return new StockDeductResult(false, strategy, reason, beforeQty, beforeQty, false);
        }
    }

    /** 借书/续借的统一结果。ok=false 时 reason 是可直接展示给读者的中文原因。 */
    public record BorrowResult(
            boolean ok,
            Long borrowId,
            LocalDateTime dueAt,
            String reason,
            String strategy,
            boolean idempotentReplay) {

        public static BorrowResult ok(Long borrowId, LocalDateTime dueAt, String strategy) {
            return new BorrowResult(true, borrowId, dueAt, null, strategy, false);
        }

        public static BorrowResult fail(String reason) {
            return new BorrowResult(false, null, null, reason, null, false);
        }

        public BorrowResult asReplay() {
            return new BorrowResult(ok, borrowId, dueAt, reason, strategy, true);
        }
    }

    /** 读者当前在借的一条记录（"我的借阅"）。 */
    public record LoanView(
            Long borrowId,
            Long bookId,
            String title,
            String author,
            String shelfLocation,
            LocalDateTime borrowAt,
            LocalDateTime dueAt,
            int overdueDays,
            int renewCount,
            boolean renewable,
            String status,
            BigDecimal fine) {
    }

    /** 读者画像，Agent 做个性化推荐时作为上下文输入。 */
    public record ReaderProfileView(
            Long readerId,
            String cardNo,
            String name,
            String readerType,
            String dept,
            int creditScore,
            int borrowedCount,
            int maxBorrowCount,
            List<String> preferredCategories,
            List<String> recentTitles) {
    }

    /** 由 rule_config 表解析出的借阅规则快照。 */
    public record BorrowRule(
            int maxBorrowCount,
            int loanDays,
            int maxRenewCount,
            int renewDays,
            BigDecimal finePerDay,
            BigDecimal fineFreeLimit,
            int minCreditScore,
            int reservationHoldDays,
            int maxRenewDays) {
    }

    /** 推荐结果，带可解释理由（Agent 回答里会直接引用 reason）。 */
    public record RecommendItem(BookView book, double score, String reason, String source) {
    }

    /** 预约结果。queueNo 为该书目下的排队序号（从 1 开始）。 */
    public record ReservationResult(boolean ok, Long reservationId, int queueNo, String reason) {
        public static ReservationResult ok(Long id, int queueNo) {
            return new ReservationResult(true, id, queueNo, null);
        }

        public static ReservationResult fail(String reason) {
            return new ReservationResult(false, null, 0, reason);
        }
    }
}
