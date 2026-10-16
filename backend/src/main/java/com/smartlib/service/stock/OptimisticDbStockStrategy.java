package com.smartlib.service.stock;

import org.springframework.stereotype.Component;

import com.smartlib.domain.dto.Dtos.StockDeductResult;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.mapper.BookStockMapper;

/**
 * S2：数据库条件更新（默认方案）。
 *
 * <p>核心就是一条 SQL：
 * <pre>
 * update book_stock
 *    set available_copies = available_copies - #{n}, version = version + 1
 *  where book_id = #{bookId} and available_copies &gt;= #{n}
 * </pre>
 *
 * <p>为什么它是默认方案：
 * <ol>
 *   <li>正确性由数据库的行锁 + 条件判断共同保证，不依赖应用层的任何假设；</li>
 *   <li>不引入 Redis/MQ 等额外组件，少一个组件就少一类故障与一致性问题；</li>
 *   <li>影响行数天然就是"是否扣减成功"的判据，无需先查后改，没有 check-then-act 竞态；</li>
 *   <li>可读性极高，评审、排障、交接成本最低。</li>
 * </ol>
 *
 * <p>它的代价是高并发下同一行的锁竞争（会串行化），这在"热门书瞬时并发"这个真实场景下
 * 是可接受的：热点集中在极少数行，并且这正是我们需要的语义。
 * 需要进一步抗量时才切 S5（Redis+Lua 预扣），见 ADR-004。
 */
@Component
public class OptimisticDbStockStrategy implements StockStrategy {

    public static final String NAME = "optimistic-db";

    private final BookStockMapper stockMapper;

    public OptimisticDbStockStrategy(BookStockMapper stockMapper) {
        this.stockMapper = stockMapper;
    }

    @Override
    public String name() {
        return NAME;
    }

    @Override
    public StockDeductResult tryDeduct(long bookId, int quantity, String bizNo) {
        BookStock before = stockMapper.findByBookId(bookId);
        if (before == null) {
            return StockDeductResult.fail(NAME, "书目不存在库存记录", 0);
        }
        int beforeQty = before.getAvailableCopies();

        // 先做一次无锁预判，绝大多数"库存为 0"的请求在这一步就返回，不必去打数据库行锁
        if (beforeQty < quantity) {
            return StockDeductResult.fail(NAME, "库存不足", beforeQty);
        }

        // 真正的裁决在这一条条件更新上：影响行数为 0 即竞争失败，绝不可能扣成负数
        if (stockMapper.deductConditional(bookId, quantity) == 0) {
            return StockDeductResult.fail(NAME, "库存不足", beforeQty);
        }
        return StockDeductResult.ok(NAME, beforeQty, beforeQty - quantity);
    }
}
