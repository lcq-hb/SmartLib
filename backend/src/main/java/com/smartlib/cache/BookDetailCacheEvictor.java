package com.smartlib.cache;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import com.smartlib.service.BookService;
import com.smartlib.service.StockService;

/**
 * 库存变化时失效书目详情缓存。
 *
 * <p><b>这个类是为了修一个真实缺陷而存在的。</b>压测中发现：
 * 借出 8 册之后立刻请求 {@code GET /api/books/{id}}，返回的 {@code availableCopies} 仍然是 8，
 * 而直读数据库的库存接口返回的是正确的 0；30 个压测轮次每一轮都能复现。
 *
 * <p><b>根因</b>：{@link BookService#getById} 把含 {@code availableCopies} 的 {@code BookView}
 * 放进了 TTL 10 分钟的缓存，而当时整个后端<b>唯一</b>的
 * {@link StockService.StockChangeListener} 实现是倒排索引引擎（它只更新索引里的库存快照），
 * <b>没有任何代码去失效书目详情缓存</b>。于是"可借数"最多会偏旧 10 分钟 ——
 * 对一个"读者点进去看能不能借"的页面来说，这个偏差足以造成误导。
 *
 * <p><b>为什么单独做成一个监听器而不是塞进倒排索引里</b>：
 * 两件事的职责不同 —— 索引引擎关心"检索结果里的库存快照"，本类关心"详情接口的缓存"。
 * 分成两个 listener 后，{@code StockService} 只负责广播"这本书的库存变了"，
 * 谁需要反应谁自己订阅（现在有两个订阅者，将来加"库存告警推送"就是第三个）。
 *
 * <p><b>它解决不了的部分（必须诚实说明）</b>：多实例部署时，
 * 本类只能清掉<b>当前实例</b>的 L1 缓存与共享的 L2。
 * 其他实例的 L1 里的旧值只能等 TTL 自然过期 ——
 * 这正是 {@code BookService.DETAIL_TTL} 被压到 30 秒的原因：
 * <b>凡是包含易变字段（库存）的缓存，TTL 必须按"可接受的偏旧时长"来定，
 * 而不是按"数据变更频率"来定。</b>
 */
@Component
public class BookDetailCacheEvictor implements StockService.StockChangeListener {

    private static final Logger log = LoggerFactory.getLogger(BookDetailCacheEvictor.class);

    private final CacheGuard cacheGuard;

    public BookDetailCacheEvictor(CacheGuard cacheGuard) {
        this.cacheGuard = cacheGuard;
    }

    @Override
    public void onStockChanged(long bookId) {
        String key = BookService.DETAIL_KEY_PREFIX + bookId;
        cacheGuard.evict(key);
        log.debug("已失效书目详情缓存 key={}（库存变化）", key);
    }
}
