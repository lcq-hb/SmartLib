package com.smartlib.service;

import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import com.smartlib.cache.BookIdBloomFilter;
import com.smartlib.cache.CacheGuard;
import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;
import com.smartlib.common.PageResult;
import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.BookRow;
import com.smartlib.domain.dto.Dtos.BookView;
import com.smartlib.domain.dto.Dtos.SearchResult;
import com.smartlib.domain.entity.Book;
import com.smartlib.mapper.BookMapper;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * 书目服务：详情、热门、相似、新建。
 *
 * <p><b>防缓存穿透的三层组合</b>（对应解决方案手册 P3）：
 * <ol>
 *   <li>布隆过滤器：判断"一定不存在"的 id，直接 404，不碰缓存也不碰数据库；</li>
 *   <li>空值缓存：布隆的假阳性会让少量不存在的 id 走到数据库，
 *       这时把"空结果"缓存 60 秒，避免反复穿透；</li>
 *   <li>单飞：热点书详情缓存失效时，只放行一个请求回源。</li>
 * </ol>
 */
@Service
public class BookService {

    /**
     * 书目详情缓存 key 前缀。公开为常量是为了让
     * {@code BookDetailCacheEvictor} 失效缓存时不会因为字符串不一致而静默失效
     * （拼错的 key 不会报错，只会让缓存永远清不掉 —— 这类 bug 极难发现）。
     */
    public static final String DETAIL_KEY_PREFIX = "smartlib:book:";

    /**
     * 书目详情缓存时长。
     *
     * <p><b>为什么只有 30 秒</b>：这个缓存里含有 {@code availableCopies} 这种易变字段。
     * TTL 的取值依据不是"书目本身多久变一次"（那确实可以是几小时），
     * 而是"读者能容忍看到多旧的库存"。压测实测过：TTL 为 10 分钟时，
     * 借出 8 册后详情接口仍返回 8，读者会白跑一趟。
     *
     * <p>库存变化时 {@code BookDetailCacheEvictor} 会主动失效当前实例的缓存，
     * 所以 30 秒这个上界只在"多实例部署、且变更发生在别的实例上"时才生效
     * （进程内 L1 缓存无法被其他实例清除）。这是一个明确的取舍：
     * 用缓存命中率换"可接受的偏旧时长"，而不是假装缓存能实时一致。
     */
    private static final Duration DETAIL_TTL = Duration.ofSeconds(30);

    private static final Duration HOT_TTL = Duration.ofMinutes(1);
    /** 热门榜统计窗口：近 90 天。 */
    private static final int HOT_WINDOW_DAYS = 90;

    private final BookMapper bookMapper;
    private final CacheGuard cacheGuard;
    private final BookIdBloomFilter bloomFilter;
    private final BookSearchService bookSearchService;
    private final MeterRegistry meterRegistry;

    public BookService(BookMapper bookMapper,
                       CacheGuard cacheGuard,
                       BookIdBloomFilter bloomFilter,
                       BookSearchService bookSearchService,
                       MeterRegistry meterRegistry) {
        this.bookMapper = bookMapper;
        this.cacheGuard = cacheGuard;
        this.bloomFilter = bloomFilter;
        this.bookSearchService = bookSearchService;
        this.meterRegistry = meterRegistry;
    }

    @Transactional(propagation = Propagation.SUPPORTS, readOnly = true)
    public BookView getById(long bookId) {
        if (!bloomFilter.mightExist(bookId)) {
            // 布隆说"一定不存在"，这一步省下了一次缓存查询 + 一次数据库查询
            meterRegistry.counter("smartlib.book.detail", "result", "bloom-rejected").increment();
            throw new BizException(ErrorCode.BOOK_NOT_FOUND, "图书不存在：id=" + bookId);
        }
        BookView view = cacheGuard.get(DETAIL_KEY_PREFIX + bookId, DETAIL_TTL, BookView.class, () -> loadDetail(bookId));
        if (view == null) {
            meterRegistry.counter("smartlib.book.detail", "result", "not-found").increment();
            throw new BizException(ErrorCode.BOOK_NOT_FOUND, "图书不存在：id=" + bookId);
        }
        meterRegistry.counter("smartlib.book.detail", "result", "ok").increment();
        return view;
    }

    private BookView loadDetail(long bookId) {
        List<BookRow> rows = bookMapper.findRowsByIds(List.of(bookId));
        return rows.isEmpty() ? null : com.smartlib.service.search.BookViews.toView(rows.get(0));
    }

    @Transactional(propagation = Propagation.SUPPORTS, readOnly = true)
    public List<BookView> hot(int limit) {
        int size = Math.min(Math.max(limit, 1), 50);
        List<BookRow> rows = bookMapper.findHot(LocalDateTime.now().minusDays(HOT_WINDOW_DAYS), size);
        return rows.stream().map(com.smartlib.service.search.BookViews::toView).toList();
    }

    @Transactional(propagation = Propagation.SUPPORTS, readOnly = true)
    public List<BookView> similar(long bookId, int limit) {
        Book book = bookMapper.findById(bookId);
        if (book == null) {
            throw new BizException(ErrorCode.BOOK_NOT_FOUND, "图书不存在：id=" + bookId);
        }
        int size = Math.min(Math.max(limit, 1), 20);
        List<BookRow> rows = bookMapper.findSimilar(book.getCategoryId(), List.of(bookId),
                LocalDateTime.now().minusDays(HOT_WINDOW_DAYS), size);
        return rows.stream().map(com.smartlib.service.search.BookViews::toView).toList();
    }

    @Transactional(readOnly = true)
    public PageResult<BookView> search(BookQuery query) {
        SearchResult result = bookSearchService.search(query);
        return PageResult.of(result.items(), result.total(), query.getPage(), query.getSize());
    }

    /**
     * 新建书目。
     *
     * <p>注意这里必须调用 {@code bloomFilter.addBook}：
     * 漏掉这一行的后果是"新上架的书永远 404"（布隆的假阴性），
     * 而这个 bug 在测试环境很难发现（因为重启后重建过滤器就正常了）。
     */
    @Transactional(rollbackFor = Exception.class)
    public long create(Book book) {
        book.setTotalCopies(0);
        bookMapper.insert(book);
        bloomFilter.addBook(book.getId());
        return book.getId();
    }
}
