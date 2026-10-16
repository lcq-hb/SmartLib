package com.smartlib.service.stock;

import org.springframework.stereotype.Component;

import com.smartlib.domain.dto.Dtos.StockDeductResult;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.mapper.BookStockMapper;

/**
 * S3：数据库悲观锁（{@code select ... for update}）。
 *
 * <p>与 S2 的区别在于"先锁定再判断"：进入方法后立刻对目标行加排他锁，
 * 同一行的其他事务会在此阻塞，直到本事务提交。
 *
 * <p>因此有两个必须遵守的约束（也是评审时被重点质疑的点）：
 * <ol>
 *   <li><b>必须在事务中调用</b>：{@code for update} 的锁在事务提交/回滚时释放，
 *       若没有事务包裹，加锁与释放会发生在同一条语句里，锁形同虚设；</li>
 *   <li><b>锁内禁止任何耗时操作</b>：发消息、调风控、写日志到远程都会把锁持有时间
 *       从毫秒级放大到百毫秒级，热点行会迅速堆积成连接池耗尽。</li>
 * </ol>
 *
 * <p>它适合"扣减逻辑复杂、需要多次读写同一行"的场景；单纯扣减库存时，
 * 一条条件更新（S2）能以更低代价得到同样的正确性。
 */
@Component
public class PessimisticDbStockStrategy implements StockStrategy {

    public static final String NAME = "pessimistic-db";

    private final BookStockMapper stockMapper;

    public PessimisticDbStockStrategy(BookStockMapper stockMapper) {
        this.stockMapper = stockMapper;
    }

    @Override
    public String name() {
        return NAME;
    }

    @Override
    public StockDeductResult tryDeduct(long bookId, int quantity, String bizNo) {
        // 加排他锁：此后到事务提交前，其他事务无法修改这一行
        BookStock locked = stockMapper.selectForUpdate(bookId);
        if (locked == null) {
            return StockDeductResult.fail(NAME, "书目不存在库存记录", 0);
        }
        int beforeQty = locked.getAvailableCopies();
        if (beforeQty < quantity) {
            return StockDeductResult.fail(NAME, "库存不足", beforeQty);
        }
        // 即使已持锁，仍然保留条件判断：防御性编程，避免未来有人把锁去掉而静默引入超卖
        if (stockMapper.deductConditional(bookId, quantity) == 0) {
            return StockDeductResult.fail(NAME, "库存不足", beforeQty);
        }
        return StockDeductResult.ok(NAME, beforeQty, beforeQty - quantity);
    }
}
