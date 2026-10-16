package com.smartlib.cache;

import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import com.smartlib.domain.dto.BookRow;
import com.smartlib.mapper.BookMapper;

import jakarta.annotation.PostConstruct;

/**
 * 书目 id 布隆过滤器 —— 防缓存穿透的前置闸门。
 *
 * <p>构建方式：启动时把全量书目 id 灌进过滤器（分页读取，避免一次性加载）。
 * 之后每次按 id 查详情，先问一次布隆：
 * <ul>
 *   <li>回答"一定不存在" → 直接返回 404，<b>不查缓存也不查数据库</b>；</li>
 *   <li>回答"可能存在" → 走正常的缓存 → 数据库流程。</li>
 * </ul>
 *
 * <p><b>假阴性的风险与规避（这是本类最关键的设计约束）</b>：
 * 布隆过滤器本身没有假阴性，但"新增书目后忘记 add"会造成假阴性 ——
 * 那会让一本真实存在的书对读者永远不可见，属于严重功能缺陷。
 * 因此 {@link #addBook(long)} 必须被所有新增书目的代码路径调用，
 * 并且在 {@code BookService.createBook} 里强制执行。
 */
@Component
public class BookIdBloomFilter {

    private static final Logger log = LoggerFactory.getLogger(BookIdBloomFilter.class);

    private static final int BUILD_BATCH_SIZE = 5000;

    private final BookMapper bookMapper;
    private final boolean enabled;
    private final long expectedInsertions;
    private final double falsePositiveRate;
    private final int maxDocs;

    private volatile BloomFilter filter;

    public BookIdBloomFilter(BookMapper bookMapper,
                             @Value("${smartlib.search.cache.bloom-enabled:true}") boolean enabled,
                             @Value("${smartlib.search.cache.bloom-expected-insertions:1200000}") long expectedInsertions,
                             @Value("${smartlib.search.cache.bloom-fpp:0.001}") double falsePositiveRate,
                             @Value("${smartlib.search.index.max-docs:500000}") int maxDocs) {
        this.bookMapper = bookMapper;
        this.enabled = enabled;
        this.expectedInsertions = expectedInsertions;
        this.falsePositiveRate = falsePositiveRate;
        this.maxDocs = maxDocs;
    }

    @PostConstruct
    public void build() {
        if (!enabled) {
            log.warn("布隆过滤器已关闭（smartlib.search.cache.bloom-enabled=false），"
                    + "缓存穿透保护将只依赖空值缓存");
            this.filter = null;
            return;
        }
        long start = System.currentTimeMillis();
        BloomFilter created = new BloomFilter(expectedInsertions, falsePositiveRate);
        int offset = 0;
        int loaded = 0;
        while (loaded < maxDocs) {
            List<BookRow> batch = bookMapper.selectPageForIndex(offset, BUILD_BATCH_SIZE);
            if (batch.isEmpty()) {
                break;
            }
            for (BookRow row : batch) {
                if (row.getId() != null) {
                    created.add(key(row.getId()));
                }
            }
            loaded += batch.size();
            offset += batch.size();
            if (batch.size() < BUILD_BATCH_SIZE) {
                break;
            }
        }
        this.filter = created;
        log.info("{} 构建完成，耗时 {}ms", created.describe(), System.currentTimeMillis() - start);
    }

    /**
     * @return false 表示该 id <b>一定不存在</b>，可以安全地直接拒绝
     */
    public boolean mightExist(long bookId) {
        BloomFilter snapshot = this.filter;
        if (snapshot == null) {
            // 未启用时一律回答"可能存在"，让请求走正常流程（保守但不丢功能）
            return true;
        }
        return snapshot.mightContain(key(bookId));
    }

    /** 新增书目后必须调用，否则该书的详情页会因为假阴性而 404。 */
    public void addBook(long bookId) {
        BloomFilter snapshot = this.filter;
        if (snapshot != null) {
            snapshot.add(key(bookId));
        }
    }

    public boolean enabled() {
        return filter != null;
    }

    public String describe() {
        BloomFilter snapshot = this.filter;
        return snapshot == null ? "布隆过滤器未启用" : snapshot.describe();
    }

    private String key(long bookId) {
        return "book:" + bookId;
    }
}
