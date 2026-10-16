package com.smartlib.service.stock;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.Future;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicInteger;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

import com.smartlib.common.TraceContext;
import com.smartlib.domain.dto.Dtos.StockDeductResult;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.domain.entity.StockLedger;
import com.smartlib.mapper.BookStockMapper;
import com.smartlib.mapper.StockLedgerMapper;

/**
 * S6：消息队列串行化扣减（本地以"单线程执行器 + 有界队列"模拟 MQ 的单分区顺序消费）。
 *
 * <p><b>真正的生产形态</b>是：借书请求只做校验与落单，
 * 把"扣减库存"这条消息投到以 bookId 为分区键的 MQ 上，
 * 由单分区顺序消费保证同一本书的扣减彻底串行，天然无锁竞争、可削峰填谷。
 * 本地没有 MQ，因此用一个 1 线程 + 有界队列的执行器精确模拟"MQ 顺序消费"的语义，
 * 这样接口、失败语义、超时行为都能被测到。
 *
 * <p><b>它自行写流水（{@link #writesOwnLedger()} 返回 true）</b>：
 * 扣减发生在工作线程的独立事务里，如果把流水留给调用方事务去写，
 * 一旦调用方事务回滚，就会出现"库存已扣但流水没记"的黑洞。
 * 因此这里用 {@link TransactionTemplate} 让"扣减 + 记账"在工作线程内成为一个原子单元。
 *
 * <p><b>必须承认的两个代价</b>（写在代码里而不是藏起来）：
 * <ol>
 *   <li>调用方在等待 {@code Future.get} 时会持有自己的数据库事务连接，
 *       这是典型的"事务内等远程调用"反模式。生产做法是接口返回 202 + 异步通知；
 *       这里保留同步等待是为了让压测能直接拿到"成功/失败"以便横向对比；</li>
 *   <li>队列满或等待超时时必须明确失败，不能静默丢弃，否则用户以为借到了书。</li>
 * </ol>
 */
@Component
public class MqSerialStockStrategy implements StockStrategy {

    public static final String NAME = "mq-serial";

    private static final Logger log = LoggerFactory.getLogger(MqSerialStockStrategy.class);

    private final BookStockMapper stockMapper;
    private final StockLedgerMapper ledgerMapper;
    private final TransactionTemplate transactionTemplate;
    private final ThreadPoolExecutor dispatcher;
    private final long timeoutMs;

    public MqSerialStockStrategy(BookStockMapper stockMapper,
                                 StockLedgerMapper ledgerMapper,
                                 PlatformTransactionManager transactionManager,
                                 @Value("${smartlib.stock.mq.queue-capacity:20000}") int queueCapacity,
                                 @Value("${smartlib.stock.mq.timeout-ms:3000}") long timeoutMs) {
        this.stockMapper = stockMapper;
        this.ledgerMapper = ledgerMapper;
        this.transactionTemplate = new TransactionTemplate(transactionManager);
        this.timeoutMs = timeoutMs;
        // 单线程 = 单分区；有界队列 = 明确的背压边界；AbortPolicy = 拒绝时立刻失败而不是无限堆积
        this.dispatcher = new ThreadPoolExecutor(
                1, 1, 0L, TimeUnit.MILLISECONDS,
                new ArrayBlockingQueue<>(queueCapacity),
                namedFactory("smartlib-stock-dispatcher"),
                new ThreadPoolExecutor.AbortPolicy());
    }

    private static ThreadFactory namedFactory(String prefix) {
        AtomicInteger seq = new AtomicInteger();
        return runnable -> {
            Thread t = new Thread(runnable, prefix + "-" + seq.incrementAndGet());
            t.setDaemon(true);
            return t;
        };
    }

    @Override
    public String name() {
        return NAME;
    }

    @Override
    public boolean writesOwnLedger() {
        return true;
    }

    /** 当前排队中的扣减任务数，压测与监控用（对应 MQ 的堆积量）。 */
    public int queueSize() {
        return dispatcher.getQueue().size();
    }

    @Override
    public StockDeductResult tryDeduct(long bookId, int quantity, String bizNo) {
        String traceId = TraceContext.get();
        Future<StockDeductResult> future;
        try {
            future = dispatcher.submit(() -> transactionTemplate.execute(status ->
                    deductAndLedger(bookId, quantity, bizNo, traceId)));
        } catch (RejectedExecutionException e) {
            // 队列已满：明确的背压信号，必须失败而不是丢弃
            return StockDeductResult.fail(NAME, "扣减队列已满，请稍后重试", 0);
        }

        try {
            return future.get(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (TimeoutException e) {
            return handleTimeout(future, bookId, bizNo);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return StockDeductResult.fail(NAME, "扣减被中断", 0);
        } catch (Exception e) {
            log.error("扣减任务执行异常 bookId={} bizNo={}", bookId, bizNo, e);
            return StockDeductResult.fail(NAME, "扣减执行异常", 0);
        }
    }

    /**
     * 超时处理 —— <b>这里是本策略最容易写错、也最危险的地方</b>。
     *
     * <p>如果超时后直接返回"失败"，就会出现最糟糕的不一致：
     * 任务其实已经（或即将）完成扣减与记账，但调用方告诉读者"借书失败"，
     * 于是库存少了一册而没人认领。本项目在 200 并发压测里真实触发过这个问题
     * （报成功 8 次、实际扣减 10 次）。
     *
     * <p>正确的处理是区分两种情况：
     * <ul>
     *   <li>{@code cancel(false)} 返回 <b>true</b>：任务还在队列里没开始执行，
     *       取消是有效的，可以安全地报失败；</li>
     *   <li>返回 <b>false</b>：任务已经开始执行，取消无效 —— 此时<b>必须等它给出确定结果</b>。
     *       副作用已经发生，任何"当作失败"的处理都是在制造脏数据。</li>
     * </ul>
     *
     * <p>这也正是生产环境不应该用同步等待消费结果的原因：
     * 正确的形态是接口返回 202 + 异步通知。本项目保留同步语义只是为了能横向压测对比，
     * 边界已记录在 docs/09-多种解决方案对比手册.md 的踩坑记录里。
     */
    private StockDeductResult handleTimeout(Future<StockDeductResult> future, long bookId, String bizNo) {
        if (future.cancel(false)) {
            log.warn("扣减任务排队超时且成功取消 bookId={} bizNo={}", bookId, bizNo);
            return StockDeductResult.fail(NAME, "扣减排队超时，请稍后重试", 0);
        }
        log.warn("扣减任务已开始执行，取消无效，等待其给出确定结果 bookId={} bizNo={}", bookId, bizNo);
        try {
            return future.get();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return StockDeductResult.fail(NAME, "等待扣减结果时被中断，结果未知", 0);
        } catch (Exception e) {
            log.error("等待扣减结果失败 bookId={} bizNo={}", bookId, bizNo, e);
            return StockDeductResult.fail(NAME, "扣减结果未知", 0);
        }
    }

    /**
     * 在工作线程的独立事务中完成"条件扣减 + 写流水"。
     * 注意这是在另一个连接上执行，与调用方事务互不影响 —— 这正是 MQ 方案的语义。
     */
    private StockDeductResult deductAndLedger(long bookId, int quantity, String bizNo, String traceId) {
        BookStock before = stockMapper.findByBookId(bookId);
        if (before == null) {
            return StockDeductResult.fail(NAME, "书目不存在库存记录", 0);
        }
        int beforeQty = before.getAvailableCopies();
        if (stockMapper.deductConditional(bookId, quantity) == 0) {
            return StockDeductResult.fail(NAME, "库存不足", beforeQty);
        }
        int afterQty = beforeQty - quantity;

        StockLedger ledger = new StockLedger();
        ledger.setBookId(bookId);
        ledger.setOpType("DEDUCT");
        ledger.setDelta(-quantity);
        ledger.setBeforeQty(beforeQty);
        ledger.setAfterQty(afterQty);
        ledger.setBizNo(bizNo);
        ledger.setStrategy(NAME);
        ledger.setTraceId(traceId);
        ledgerMapper.insert(ledger);

        return StockDeductResult.okWithLedger(NAME, beforeQty, afterQty);
    }
}
