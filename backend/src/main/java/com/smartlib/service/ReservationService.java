package com.smartlib.service;

import java.time.LocalDateTime;
import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;
import com.smartlib.domain.dto.Dtos.ReservationResult;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.domain.entity.Reader;
import com.smartlib.domain.entity.Reservation;
import com.smartlib.mapper.BookStockMapper;
import com.smartlib.mapper.ReaderMapper;
import com.smartlib.mapper.ReservationMapper;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * 预约服务：排队、取消、到馆保留与过期出队。
 *
 * <p><b>业务规则</b>（蓝图 §2.2 第 7 条）：
 * <ul>
 *   <li>只有"可借数为 0"时才允许预约（有库存直接借即可，预约反而让读者多等）；</li>
 *   <li>同一读者对同一书目只能有一个有效预约；</li>
 *   <li>队列 FIFO，序号由 {@code max(queue_no)+1} 生成；</li>
 *   <li>到馆后保留 3 天（可在 rule_config 调整），到期未取自动出队并顺延给下一位。</li>
 * </ul>
 *
 * <p><b>并发注意</b>：取号与插入必须在同一事务内，否则两个并发预约会拿到同一个 queue_no。
 * 严格来说还应该在 {@code (book_id, status, queue_no)} 上加唯一约束来兜底，
 * 这里因为要在 H2 与 MySQL 之间保持 DDL 兼容而没有加，属于已知取舍（见技术债清单）。
 */
@Service
public class ReservationService {

    private static final Logger log = LoggerFactory.getLogger(ReservationService.class);

    private final ReservationMapper reservationMapper;
    private final BookStockMapper stockMapper;
    private final ReaderMapper readerMapper;
    private final RuleEngine ruleEngine;
    private final MeterRegistry meterRegistry;

    public ReservationService(ReservationMapper reservationMapper,
                              BookStockMapper stockMapper,
                              ReaderMapper readerMapper,
                              RuleEngine ruleEngine,
                              MeterRegistry meterRegistry) {
        this.reservationMapper = reservationMapper;
        this.stockMapper = stockMapper;
        this.readerMapper = readerMapper;
        this.ruleEngine = ruleEngine;
        this.meterRegistry = meterRegistry;
    }

    @Transactional(rollbackFor = Exception.class)
    public ReservationResult reserve(long readerId, long bookId) {
        Reader reader = readerMapper.findById(readerId);
        if (reader == null) {
            throw new BizException(ErrorCode.READER_NOT_FOUND, "读者不存在：id=" + readerId);
        }
        BookStock stock = stockMapper.findByBookId(bookId);
        if (stock == null) {
            throw new BizException(ErrorCode.BOOK_NOT_FOUND, "图书不存在：id=" + bookId);
        }
        if (stock.getAvailableCopies() > 0) {
            return ReservationResult.fail("该书当前有可借复本，请直接借阅");
        }
        if (reservationMapper.countActiveByReaderAndBook(readerId, bookId) > 0) {
            throw new BizException(ErrorCode.RESERVATION_DUPLICATED);
        }

        int queueNo = reservationMapper.maxQueueNo(bookId) + 1;
        Reservation reservation = new Reservation();
        reservation.setReaderId(readerId);
        reservation.setBookId(bookId);
        reservation.setQueueNo(queueNo);
        reservation.setStatus("WAITING");
        reservationMapper.insert(reservation);

        meterRegistry.counter("smartlib.reservation", "result", "created").increment();
        log.info("预约成功 readerId={} bookId={} queueNo={}", readerId, bookId, queueNo);
        return ReservationResult.ok(reservation.getId(), queueNo);
    }

    @Transactional(rollbackFor = Exception.class)
    public void cancel(long readerId, long reservationId) {
        Reservation reservation = reservationMapper.findById(reservationId);
        if (reservation == null) {
            throw new BizException(ErrorCode.RESERVATION_NOT_FOUND);
        }
        if (reservation.getReaderId() != readerId) {
            throw new BizException(ErrorCode.FORBIDDEN, "只能取消本人的预约");
        }
        if (!"WAITING".equals(reservation.getStatus()) && !"READY".equals(reservation.getStatus())) {
            throw new BizException(ErrorCode.RESERVATION_NOT_FOUND, "该预约已结束，无法取消");
        }
        boolean wasReady = "READY".equals(reservation.getStatus());
        reservationMapper.updateStatus(reservationId, "CANCELLED", null, null);
        meterRegistry.counter("smartlib.reservation", "result", "cancelled").increment();

        // 如果取消的是"已到馆待取"的预约，名额要顺延给下一位，否则书会白白空等
        if (wasReady) {
            promoteNext(reservation.getBookId());
        }
    }

    /**
     * 到馆保留超期处理：由定时任务调用。
     *
     * @return 处理条数
     */
    @Transactional(rollbackFor = Exception.class)
    public int expireReadyReservations(int batchSize) {
        List<Reservation> expired = reservationMapper.findExpiredReady(LocalDateTime.now(), batchSize);
        int handled = 0;
        for (Reservation reservation : expired) {
            reservationMapper.updateStatus(reservation.getId(), "EXPIRED", null, null);
            // 到馆未取，扣信用分（蓝图 §2.2 第 9 条）
            readerMapper.addCreditScore(reservation.getReaderId(), -3);
            promoteNext(reservation.getBookId());
            handled++;
            log.info("预约过期出队 reservationId={} readerId={} bookId={}",
                    reservation.getId(), reservation.getReaderId(), reservation.getBookId());
        }
        if (handled > 0) {
            meterRegistry.counter("smartlib.reservation", "result", "expired").increment(handled);
        }
        return handled;
    }

    /**
     * 把队列中的下一位置为"到馆保留中"。
     *
     * <p>只有确实有可借复本时才推进，否则会出现"通知了读者却没有书"的尴尬。
     */
    @Transactional(rollbackFor = Exception.class)
    public void promoteNext(long bookId) {
        BookStock stock = stockMapper.findByBookId(bookId);
        if (stock == null || stock.getAvailableCopies() <= 0) {
            return;
        }
        Reservation head = reservationMapper.findQueueHead(bookId);
        if (head == null) {
            return;
        }
        Reader reader = readerMapper.findById(head.getReaderId());
        int holdDays = ruleEngine.ruleFor(reader == null ? "*" : reader.getType()).reservationHoldDays();
        LocalDateTime now = LocalDateTime.now();
        reservationMapper.updateStatus(head.getId(), "READY", now, now.plusDays(holdDays));
        log.info("预约到馆保留：bookId={} reservationId={} readerId={} 保留 {} 天",
                bookId, head.getId(), head.getReaderId(), holdDays);
    }

    @Transactional(readOnly = true)
    public List<Reservation> listActive(long readerId) {
        return reservationMapper.listActiveByReader(readerId);
    }
}
