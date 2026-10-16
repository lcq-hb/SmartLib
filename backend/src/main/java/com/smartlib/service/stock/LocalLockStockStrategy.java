package com.smartlib.service.stock;

import java.util.concurrent.locks.ReentrantLock;

import org.springframework.stereotype.Component;

import com.smartlib.domain.dto.Dtos.StockDeductResult;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.mapper.BookStockMapper;

/**
 * S1：单机锁策略。
 *
 * <p><b>它存在的意义主要是教学与对照：</b>很多人在遇到超卖时的第一反应是"加 synchronized"，
 * 这个策略把那条思路完整实现出来，然后用压测数据说明它的边界在哪。
 *
 * <p>关键实现细节：使用<b>分段锁（striped lock）</b>而不是
 * {@code ConcurrentHashMap<Long, Lock>}。后者在 120 万书目下最坏会常驻上百万个锁对象，
 * 是无界内存增长；而按 {@code bookId % 64} 映射到固定 64 个锁，内存恒定，
 * 代价是不同书目之间会偶发争抢（可接受，因为本来就不是生产方案）。
 *
 * <p>失效场景：多实例部署时，两个 JVM 各持有自己的锁，互不感知，
 * 超卖会重新出现 —— 这是它不能作为生产方案的唯一原因，也是必须写进文档的结论。
 */
@Component
public class LocalLockStockStrategy implements StockStrategy {

    public static final String NAME = "local-lock";

    /** 分段锁数量：2 的幂，取模可用位运算，且能被 JIT 优化。 */
    private static final int STRIPE_COUNT = 64;
    private static final int STRIPE_MASK = STRIPE_COUNT - 1;

    private final ReentrantLock[] stripes = new ReentrantLock[STRIPE_COUNT];
    private final BookStockMapper stockMapper;

    public LocalLockStockStrategy(BookStockMapper stockMapper) {
        this.stockMapper = stockMapper;
        for (int i = 0; i < STRIPE_COUNT; i++) {
            stripes[i] = new ReentrantLock();
        }
    }

    @Override
    public String name() {
        return NAME;
    }

    @Override
    public boolean multiInstanceSafe() {
        return false;
    }

    @Override
    public StockDeductResult tryDeduct(long bookId, int quantity, String bizNo) {
        ReentrantLock lock = stripes[(int) (bookId & STRIPE_MASK)];
        lock.lock();
        try {
            // 注意：锁内只做一次本地数据库更新，不做任何 RPC/IO 等待，避免锁持有时间被放大
            BookStock before = stockMapper.findByBookId(bookId);
            if (before == null) {
                return StockDeductResult.fail(NAME, "书目不存在库存记录", 0);
            }
            int beforeQty = before.getAvailableCopies();
            if (beforeQty < quantity) {
                return StockDeductResult.fail(NAME, "库存不足", beforeQty);
            }
            if (stockMapper.deductConditional(bookId, quantity) == 0) {
                return StockDeductResult.fail(NAME, "库存不足", beforeQty);
            }
            return StockDeductResult.ok(NAME, beforeQty, beforeQty - quantity);
        } finally {
            lock.unlock();
        }
    }
}
