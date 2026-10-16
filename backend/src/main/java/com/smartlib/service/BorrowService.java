package com.smartlib.service;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.Duration;
import java.time.LocalDateTime;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;
import com.smartlib.domain.dto.Dtos.BorrowResult;
import com.smartlib.domain.dto.Dtos.BorrowRule;
import com.smartlib.domain.dto.Dtos.StockDeductResult;
import com.smartlib.domain.entity.Book;
import com.smartlib.domain.entity.BorrowRecord;
import com.smartlib.domain.entity.Reader;
import com.smartlib.mapper.BookMapper;
import com.smartlib.mapper.BorrowRecordMapper;
import com.smartlib.mapper.ReaderMapper;
import com.smartlib.mapper.ReservationMapper;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * 借阅服务：借书、还书、续借。
 *
 * <p><b>借书的完整链路</b>（也是 docs/03 详细设计里那张时序图的实现）：
 * <pre>
 *   幂等占位 → 读者/书目存在性 → 规则校验 → 库存扣减（策略 SPI）
 *   → 写库存流水 → 写借阅记录 → 提交（全部在一个事务里）
 * </pre>
 *
 * <p><b>为什么整个借书是一个事务</b>：库存扣减与借阅记录必须同生共死。
 * 如果库存扣了但记录没写成，读者就凭空少了一本书的额度；
 * 如果记录写了但库存没扣，就会出现超卖。两者必须原子。
 *
 * <p>唯一的例外是 S6（MQ 串行扣减）策略：它的扣减发生在上游的独立事务里，
 * 因此语义上变成"扣减已提交，记录随后写入"，一致性由流程编排保证。
 * 这是选择 MQ 方案必须接受的代价，已在解决方案手册里明确写出。
 */
@Service
public class BorrowService {

    private static final Logger log = LoggerFactory.getLogger(BorrowService.class);

    private final BorrowRecordMapper borrowMapper;
    private final BookMapper bookMapper;
    private final ReaderMapper readerMapper;
    private final ReservationMapper reservationMapper;
    private final RuleEngine ruleEngine;
    private final StockService stockService;
    private final IdempotencyService idempotencyService;
    private final MeterRegistry meterRegistry;

    public BorrowService(BorrowRecordMapper borrowMapper,
                         BookMapper bookMapper,
                         ReaderMapper readerMapper,
                         ReservationMapper reservationMapper,
                         RuleEngine ruleEngine,
                         StockService stockService,
                         IdempotencyService idempotencyService,
                         MeterRegistry meterRegistry) {
        this.borrowMapper = borrowMapper;
        this.bookMapper = bookMapper;
        this.readerMapper = readerMapper;
        this.reservationMapper = reservationMapper;
        this.ruleEngine = ruleEngine;
        this.stockService = stockService;
        this.idempotencyService = idempotencyService;
        this.meterRegistry = meterRegistry;
    }

    /**
     * 借书。
     *
     * @param idemKey 幂等键，来自请求头 Idempotency-Key，必填
     */
    @Transactional(rollbackFor = Exception.class)
    public BorrowResult borrow(long readerId, long bookId, String idemKey) {
        // 幂等键同时作为库存流水号：这样"同一笔借阅"在幂等表和库存流水表里是同一个标识，
        // 出问题时可以用一个 key 把两张表串起来查。
        String bizNo = "BORROW:" + idemKey;

        IdempotencyService.Outcome<BorrowResult> outcome = idempotencyService.execute(
                idemKey, "BORROW", BorrowResult.class, () -> doBorrow(readerId, bookId, bizNo));

        BorrowResult result = outcome.value();
        if (outcome.replayed()) {
            meterRegistry.counter("smartlib.borrow", "result", "idempotent-replay").increment();
            return result.asReplay();
        }
        meterRegistry.counter("smartlib.borrow", "result", result.ok() ? "success" : "fail").increment();
        return result;
    }

    private BorrowResult doBorrow(long readerId, long bookId, String bizNo) {
        Reader reader = readerMapper.findById(readerId);
        if (reader == null) {
            throw new BizException(ErrorCode.READER_NOT_FOUND, "读者不存在：id=" + readerId);
        }
        Book book = bookMapper.findById(bookId);
        if (book == null) {
            throw new BizException(ErrorCode.BOOK_NOT_FOUND, "图书不存在：id=" + bookId);
        }

        // ---- 规则校验（全部来自 rule_config，改规则不发版） ----
        int activeCount = borrowMapper.countActiveByReader(readerId);
        boolean hasOverdue = borrowMapper.countOverdueByReader(readerId) > 0;
        boolean sameBookActive = borrowMapper.countActiveByReaderAndBook(readerId, bookId) > 0;
        BigDecimal unpaidFine = borrowMapper.sumUnpaidFine(readerId);
        ruleEngine.validateBorrow(reader, activeCount, hasOverdue, sameBookActive, unpaidFine);

        // ---- 库存扣减：唯一入口，具体策略由配置决定 ----
        StockDeductResult stockResult = stockService.deduct(bookId, 1, bizNo);
        if (!stockResult.success()) {
            // 业务失败用返回值表达，不抛异常：这样幂等表里会记录这次失败，
            // 客户端拿同一个 key 重试会直接得到相同的失败结果，而不是反复冲击库存。
            log.info("借书失败：库存不足 readerId={} bookId={} reason={}", readerId, bookId, stockResult.reason());
            return BorrowResult.fail(stockResult.reason());
        }

        BorrowRule rule = ruleEngine.ruleFor(reader.getType());
        LocalDateTime now = LocalDateTime.now();
        LocalDateTime dueAt = now.plusDays(rule.loanDays());

        BorrowRecord record = new BorrowRecord();
        record.setReaderId(readerId);
        record.setBookId(bookId);
        record.setBorrowAt(now);
        record.setDueAt(dueAt);
        record.setRenewCount(0);
        record.setStatus("BORROWED");
        record.setFine(BigDecimal.ZERO);
        borrowMapper.insert(record);

        log.info("借书成功 readerId={} bookId={} borrowId={} dueAt={} strategy={}",
                readerId, bookId, record.getId(), dueAt, stockResult.strategy());
        return BorrowResult.ok(record.getId(), dueAt, stockResult.strategy());
    }

    /**
     * 还书。
     *
     * <p>顺序很重要：先改借阅记录状态并计算罚金，再释放库存，最后推进预约队列。
     * 如果先释放库存再改状态，一旦中间失败就会出现"库存已回补但记录还在借"的脏数据。
     */
    @Transactional(rollbackFor = Exception.class)
    public BorrowResult returnBook(long borrowId) {
        BorrowRecord record = borrowMapper.findById(borrowId);
        if (record == null) {
            throw new BizException(ErrorCode.BORROW_RECORD_NOT_FOUND, "借阅记录不存在：id=" + borrowId);
        }
        if (!"BORROWED".equals(record.getStatus()) && !"OVERDUE".equals(record.getStatus())) {
            throw new BizException(ErrorCode.BORROW_STATUS_INVALID,
                    "当前状态[" + record.getStatus() + "]不允许归还");
        }

        LocalDateTime now = LocalDateTime.now();
        // 带状态条件的更新：并发重复归还时只有一个能成功，另一个影响行数为 0
        if (borrowMapper.markReturned(borrowId, now) == 0) {
            throw new BizException(ErrorCode.BORROW_STATUS_INVALID, "该借阅记录已被归还");
        }

        Reader reader = readerMapper.findById(record.getReaderId());
        BorrowRule rule = ruleEngine.ruleFor(reader == null ? "*" : reader.getType());
        long overdueDays = 0;
        if (record.getDueAt() != null && now.isAfter(record.getDueAt())) {
            overdueDays = Duration.between(record.getDueAt(), now).toDays();
            if (overdueDays == 0 && now.isAfter(record.getDueAt())) {
                overdueDays = 1; // 逾期不足一天也按一天计，与图书馆实际计费口径一致
            }
        }

        if (overdueDays > 0) {
            BigDecimal fine = rule.finePerDay()
                    .multiply(BigDecimal.valueOf(overdueDays))
                    .setScale(2, RoundingMode.HALF_UP);
            // 罚金封顶为书价：这是蓝图 §2.2 第 8 条的规则
            Book book = bookMapper.findById(record.getBookId());
            if (book != null && book.getPrice() != null && fine.compareTo(book.getPrice()) > 0) {
                fine = book.getPrice();
            }
            borrowMapper.updateStatusAndFine(borrowId, "RETURNED", fine);
            if (reader != null) {
                readerMapper.addCreditScore(reader.getId(), -5);
            }
            log.info("归还完成（逾期）borrowId={} 逾期 {} 天，罚金 {} 元", borrowId, overdueDays, fine);
        } else {
            borrowMapper.updateStatusAndFine(borrowId, "RETURNED", BigDecimal.ZERO);
            if (reader != null) {
                readerMapper.addCreditScore(reader.getId(), 1);
            }
            log.info("归还完成（按时）borrowId={}", borrowId);
        }

        // 释放库存：归还流水号用 borrowId 而不是幂等键，因为归还本身也是幂等的
        stockService.release(record.getBookId(), 1, "RETURN:" + borrowId);

        // 归还会让队列里的第一个人变成"可借"，必须立刻推进队列，否则书回到架上却没人通知
        promoteReservationQueue(record.getBookId());

        meterRegistry.counter("smartlib.return", "overdue", String.valueOf(overdueDays > 0)).increment();
        return BorrowResult.ok(borrowId, now, "release");
    }

    /**
     * 续借。
     */
    @Transactional(rollbackFor = Exception.class)
    public BorrowResult renew(long readerId, long borrowId) {
        BorrowRecord record = borrowMapper.findById(borrowId);
        if (record == null) {
            throw new BizException(ErrorCode.BORROW_RECORD_NOT_FOUND, "借阅记录不存在：id=" + borrowId);
        }
        // 越权校验：不能替别人续借（Agent 侧还有一层护栏，两层都要有）
        if (record.getReaderId() != readerId) {
            throw new BizException(ErrorCode.FORBIDDEN, "只能续借本人的借阅记录");
        }
        if (!"BORROWED".equals(record.getStatus()) && !"OVERDUE".equals(record.getStatus())) {
            throw new BizException(ErrorCode.BORROW_STATUS_INVALID, "当前状态不允许续借");
        }

        Reader reader = readerMapper.findById(readerId);
        if (reader == null) {
            throw new BizException(ErrorCode.READER_NOT_FOUND, "读者不存在：id=" + readerId);
        }
        BorrowRule rule = ruleEngine.ruleFor(reader.getType());
        boolean hasReservation = reservationMapper.countWaiting(record.getBookId()) > 0;
        ruleEngine.validateRenew(reader, record.getRenewCount() == null ? 0 : record.getRenewCount(), hasReservation);

        LocalDateTime baseDue = record.getDueAt() == null ? LocalDateTime.now() : record.getDueAt();
        LocalDateTime newDue = baseDue.plusDays(rule.renewDays());

        // 单册最长借阅天数上限：防止"无限续借"把一本书永久占住
        LocalDateTime maxDue = record.getBorrowAt().plusDays(rule.maxRenewDays());
        if (newDue.isAfter(maxDue)) {
            newDue = maxDue;
        }
        if (!newDue.isAfter(baseDue)) {
            throw new BizException(ErrorCode.RENEW_LIMIT_EXCEEDED,
                    "已达单册最长借阅天数 " + rule.maxRenewDays() + " 天，无法继续续借");
        }

        if (borrowMapper.markRenewed(borrowId, newDue) == 0) {
            throw new BizException(ErrorCode.BORROW_STATUS_INVALID, "续借失败，请稍后重试");
        }
        meterRegistry.counter("smartlib.renew", "result", "success").increment();
        return BorrowResult.ok(borrowId, newDue, "renew");
    }

    /**
     * 推进预约队列：把队首预约置为"到馆保留中"，并设置保留到期时间。
     * 在同一个事务里做，保证"书回到架上"与"通知下一位"不会脱节。
     */
    private void promoteReservationQueue(long bookId) {
        var head = reservationMapper.findQueueHead(bookId);
        if (head == null) {
            return;
        }
        LocalDateTime now = LocalDateTime.now();
        int holdDays = 3;
        LocalDateTime expireAt = now.plusDays(holdDays);
        reservationMapper.updateStatus(head.getId(), "READY", now, expireAt);
        log.info("预约队列推进：bookId={} reservationId={} readerId={} 保留至 {}",
                bookId, head.getId(), head.getReaderId(), expireAt);
    }
}
