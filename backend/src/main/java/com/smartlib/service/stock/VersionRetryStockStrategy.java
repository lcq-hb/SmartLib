package com.smartlib.service.stock;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import com.smartlib.domain.dto.Dtos.StockDeductResult;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.mapper.BookStockMapper;

/**
 * S4：乐观锁 CAS + 自旋重试。
 *
 * <p>流程：读出 {@code version} → 带 version 条件更新 →
 * 影响行数为 0 说明在"读"与"写"之间有人改动过这一行，重新读取并重试。
 *
 * <p>它与 S2 的关键差别值得在面试中讲清楚：
 * <ul>
 *   <li>S2 把"判断 + 更新"压缩成一条原子 SQL，数据库内部完成，不存在重试；</li>
 *   <li>S4 把判断放在应用层，需要额外的读 + 重试循环。
 *       冲突率低时它几乎不重试、吞吐更好；冲突率高时重试次数指数上升，
 *       反而比 S2 更慢 —— 这就是"热门书开抢"场景下我们没选它的原因。</li>
 * </ul>
 *
 * <p>{@link #MAX_RETRY} 设为 3：再多也只是把线程池占满，不如尽早失败让用户重试。
 * 重试耗尽后返回失败，绝不会"以为成功"。
 */
@Component
public class VersionRetryStockStrategy implements StockStrategy {

    public static final String NAME = "version-retry";

    private static final Logger log = LoggerFactory.getLogger(VersionRetryStockStrategy.class);

    /** 最大重试次数：3 次之后冲突概率仍然很高，继续自旋只会拖垮线程池。 */
    private static final int MAX_RETRY = 3;

    private final BookStockMapper stockMapper;

    public VersionRetryStockStrategy(BookStockMapper stockMapper) {
        this.stockMapper = stockMapper;
    }

    @Override
    public String name() {
        return NAME;
    }

    @Override
    public StockDeductResult tryDeduct(long bookId, int quantity, String bizNo) {
        for (int attempt = 1; attempt <= MAX_RETRY; attempt++) {
            BookStock snapshot = stockMapper.findByBookId(bookId);
            if (snapshot == null) {
                return StockDeductResult.fail(NAME, "书目不存在库存记录", 0);
            }
            int beforeQty = snapshot.getAvailableCopies();
            if (beforeQty < quantity) {
                return StockDeductResult.fail(NAME, "库存不足", beforeQty);
            }

            int affected = stockMapper.deductWithVersion(bookId, quantity, snapshot.getVersion());
            if (affected == 1) {
                return StockDeductResult.ok(NAME, beforeQty, beforeQty - quantity);
            }
            // 版本冲突：说明这一行被并发修改过，重新读取最新版本再试
            log.debug("乐观锁冲突，重试 bookId={} attempt={}/{}", bookId, attempt, MAX_RETRY);
        }
        return StockDeductResult.fail(NAME, "并发冲突重试 " + MAX_RETRY + " 次后仍未成功，请重试", 0);
    }
}
