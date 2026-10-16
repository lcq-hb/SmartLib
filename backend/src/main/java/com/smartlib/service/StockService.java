package com.smartlib.service;

import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;
import com.smartlib.common.TraceContext;
import com.smartlib.domain.dto.Dtos.StockDeductResult;
import com.smartlib.domain.dto.Dtos.StockView;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.domain.entity.StockLedger;
import com.smartlib.mapper.BookStockMapper;
import com.smartlib.mapper.ReservationMapper;
import com.smartlib.mapper.StockLedgerMapper;
import com.smartlib.service.stock.StockStrategy;
import com.smartlib.service.stock.StockStrategyFactory;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * 库存服务：所有库存变更的唯一入口。
 *
 * <p>职责边界：策略只负责"怎么安全地扣"，本服务负责：
 * <ol>
 *   <li>选择策略并调用；</li>
 *   <li>统一写库存流水（幂等表中 bizNo 唯一，天然去重）；</li>
 *   <li>打点指标，让压测与线上都能按策略维度观测成功率与耗时；</li>
 *   <li>对外提供对账能力。</li>
 * </ol>
 *
 * <p>事务说明：本方法是事务边界。为什么要在这里开事务而不是在策略里：
 * S3（悲观锁）需要事务把锁持有到提交，S2/S4 也需要扣减与流水在同一个事务里
 * 才能保证"扣了库存就一定有账"。唯一例外是 S6（MQ），它明确声明
 * {@link StockStrategy#writesOwnLedger()} = true，由它自己的事务负责记账。
 */
@Service
public class StockService {

    private static final Logger log = LoggerFactory.getLogger(StockService.class);

    /** 库存变更监听者（缓存失效等），允许没有任何实现。 */
    public interface StockChangeListener {
        void onStockChanged(long bookId);
    }

    private final StockStrategyFactory strategyFactory;
    private final BookStockMapper stockMapper;
    private final StockLedgerMapper ledgerMapper;
    private final ReservationMapper reservationMapper;
    private final MeterRegistry meterRegistry;
    private final List<StockChangeListener> listeners;
    private final TransactionTemplate transactionTemplate;

    public StockService(StockStrategyFactory strategyFactory,
                        BookStockMapper stockMapper,
                        StockLedgerMapper ledgerMapper,
                        ReservationMapper reservationMapper,
                        MeterRegistry meterRegistry,
                        List<StockChangeListener> listeners,
                        PlatformTransactionManager transactionManager) {
        this.strategyFactory = strategyFactory;
        this.stockMapper = stockMapper;
        this.ledgerMapper = ledgerMapper;
        this.reservationMapper = reservationMapper;
        this.meterRegistry = meterRegistry;
        this.listeners = listeners;
        this.transactionTemplate = new TransactionTemplate(transactionManager);
    }

    /**
     * 扣减库存。
     *
     * <p><b>这里刻意没有使用 {@code @Transactional} 注解，而是按策略决定是否开事务</b>，
     * 原因是踩过一个很隐蔽的坑：S6（MQ 串行化）策略在工作线程里用<b>独立事务</b>完成扣减，
     * 而调用方在等待结果时如果正持有一个数据库连接，200 并发下 64 个连接会被
     * "等结果的线程"全部占满，工作线程拿不到连接 → <b>连接池死锁</b>。
     * 实测表现为"报成功 8 次、实际扣减 10 次"，即库存被扣了但调用方以为失败。
     *
     * <p>因此规则是：
     * <ul>
     *   <li>策略自行记账（{@link StockStrategy#writesOwnLedger()} = true）→ <b>不开事务</b>，
     *       由策略在自己的线程里保证"扣减 + 记账"的原子性；</li>
     *   <li>其余策略 → 在事务里执行，保证"扣减 + 写流水"同生共死。</li>
     * </ul>
     *
     * <p><b>仍存在的边界</b>：如果调用方（如 {@code BorrowService.borrow}）本身处于事务中，
     * 它持有的连接不会被本方法释放，MQ 策略在高并发下依然可能耗尽连接池。
     * 生产形态必须把 MQ 扣减做成端到端异步（接口返回 202 + 异步通知），
     * 这一点已明确记录在 docs/09-多种解决方案对比手册.md 的踩坑记录中。
     *
     * @param bizNo 业务流水号，全局唯一。重复传入同一个 bizNo 视为幂等重放，直接返回成功而不重复写流水。
     */
    public StockDeductResult deduct(long bookId, int quantity, String bizNo) {
        if (quantity <= 0) {
            throw new BizException(ErrorCode.PARAM_INVALID, "扣减数量必须为正数");
        }
        StockStrategy strategy = strategyFactory.current();
        if (strategy.writesOwnLedger()) {
            return doDeduct(bookId, quantity, bizNo, strategy);
        }
        return transactionTemplate.execute(status -> doDeduct(bookId, quantity, bizNo, strategy));
    }

    private StockDeductResult doDeduct(long bookId, int quantity, String bizNo, StockStrategy strategy) {
        if (ledgerMapper.countByBizNo(bizNo) > 0) {
            log.info("库存流水已存在，判定为幂等重放 bizNo={}", bizNo);
            return StockDeductResult.ok(strategy.name(), -1, -1);
        }

        StockDeductResult result = strategy.tryDeduct(bookId, quantity, bizNo);

        if (result.success() && !result.ledgerWritten()) {
            writeLedger(bookId, "DEDUCT", -quantity, result.beforeQty(), result.afterQty(), bizNo, result.strategy());
        }
        if (result.success()) {
            notifyChanged(bookId);
        }
        recordMetrics(result);
        return result;
    }

    /**
     * 归还释放库存。归还是"加库存"，因此它不需要并发扣减那套机制，
     * 但同样要保证不超过总复本数（mapper 里带 {@code available + n <= total} 条件）。
     */
    @Transactional(rollbackFor = Exception.class)
    public void release(long bookId, int quantity, String bizNo) {
        if (quantity <= 0) {
            throw new BizException(ErrorCode.PARAM_INVALID, "释放数量必须为正数");
        }
        if (ledgerMapper.countByBizNo(bizNo) > 0) {
            log.info("归还流水已存在，判定为幂等重放 bizNo={}", bizNo);
            return;
        }
        BookStock before = stockMapper.findByBookId(bookId);
        if (before == null) {
            throw new BizException(ErrorCode.BOOK_NOT_FOUND, "书目不存在库存记录: " + bookId);
        }
        if (stockMapper.release(bookId, quantity) == 0) {
            // 说明 available + n 会超过 total，数据已经不一致了，必须暴露而不是静默忽略
            throw new BizException(ErrorCode.STOCK_DATA_INCONSISTENT,
                    "归还后可用库存将超过总复本数，请核对 bookId=" + bookId);
        }
        writeLedger(bookId, "RELEASE", quantity, before.getAvailableCopies(),
                before.getAvailableCopies() + quantity, bizNo, "release");
        notifyChanged(bookId);
    }

    /**
     * 采编入库：增加复本。
     */
    @Transactional(rollbackFor = Exception.class)
    public void addCopies(long bookId, int quantity, String bizNo) {
        BookStock before = stockMapper.findByBookId(bookId);
        if (before == null) {
            throw new BizException(ErrorCode.BOOK_NOT_FOUND, "书目不存在库存记录: " + bookId);
        }
        stockMapper.addCopies(bookId, quantity);
        stockMapper.syncBookTotalCopies(bookId, before.getTotalCopies() + quantity);
        writeLedger(bookId, "INIT", quantity, before.getAvailableCopies(),
                before.getAvailableCopies() + quantity, bizNo, "add-copies");
        notifyChanged(bookId);
    }

    @Transactional(propagation = Propagation.SUPPORTS, readOnly = true)
    public StockView view(long bookId) {
        BookStock stock = stockMapper.findByBookId(bookId);
        if (stock == null) {
            throw new BizException(ErrorCode.BOOK_NOT_FOUND, "书目不存在库存记录: " + bookId);
        }
        return new StockView(stock.getBookId(), null, stock.getTotalCopies(), stock.getAvailableCopies(),
                reservationMapper.countWaiting(bookId), null);
    }

    public int availableOf(long bookId) {
        BookStock stock = stockMapper.findByBookId(bookId);
        return stock == null ? 0 : stock.getAvailableCopies();
    }

    /**
     * 对账：返回库存与流水不守恒、以及出现负库存的书目 id。
     * 正常情况下应为空；非空说明存在绕过本服务的写路径，属于 P0 告警。
     */
    @Transactional(propagation = Propagation.SUPPORTS, readOnly = true)
    public List<Long> reconcile() {
        List<Long> violations = ledgerMapper.findViolations();
        violations.addAll(stockMapper.findNegativeStock());
        return violations.stream().distinct().toList();
    }

    private void writeLedger(long bookId, String opType, int delta, int beforeQty, int afterQty,
                             String bizNo, String strategy) {
        StockLedger ledger = new StockLedger();
        ledger.setBookId(bookId);
        ledger.setOpType(opType);
        ledger.setDelta(delta);
        ledger.setBeforeQty(beforeQty);
        ledger.setAfterQty(afterQty);
        ledger.setBizNo(bizNo);
        ledger.setStrategy(strategy);
        ledger.setTraceId(TraceContext.get());
        try {
            ledgerMapper.insert(ledger);
        } catch (DuplicateKeyException e) {
            // bizNo 唯一索引冲突：并发下同一笔业务被提交了两次。
            // 这里不抛异常，因为库存本身已经正确扣减过，重复的只是记账动作；
            // 但如果连"扣减"也重复了，就会被 report 成对账异常。
            log.warn("库存流水 bizNo 冲突，忽略重复记账 bizNo={}", bizNo);
        }
    }

    private void notifyChanged(long bookId) {
        for (StockChangeListener listener : listeners) {
            try {
                listener.onStockChanged(bookId);
            } catch (Exception e) {
                // 通知失败不能影响主流程：缓存脏了会在 TTL 到期后自愈
                log.warn("库存变更通知失败 bookId={} listener={}", bookId, listener.getClass().getSimpleName(), e);
            }
        }
    }

    private void recordMetrics(StockDeductResult result) {
        meterRegistry.counter("smartlib.stock.deduct",
                        "strategy", result.strategy() == null ? "unknown" : result.strategy(),
                        "result", result.success() ? "success" : "fail")
                .increment();
    }
}
