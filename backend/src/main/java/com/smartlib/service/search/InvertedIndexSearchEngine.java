package com.smartlib.service.search;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.BookRow;
import com.smartlib.domain.dto.Dtos.BookView;
import com.smartlib.domain.dto.Dtos.SearchResult;
import com.smartlib.mapper.BookMapper;

import jakarta.annotation.PostConstruct;

/**
 * Q3：自研内存倒排索引引擎（默认检索引擎）。
 *
 * <p><b>为什么自己写一个而不直接上 Elasticsearch</b>：
 * <ol>
 *   <li>本项目的检索规模是 120 万条书目、字段只有书名/作者/出版社/标签，
 *       单机内存完全放得下（索引约几百 MB），ES 的分布式能力用不上；</li>
 *   <li>ES 需要独立集群、分片副本规划、版本升级与 JVM 调优，运维成本远高于收益；</li>
 *   <li>自己实现能把"倒排索引 + BM25 + 二元分词 + 拼音首字母"这条链路彻底讲清楚，
 *       而不是停留在"我调了 ES 的 API"；</li>
 *   <li>ES 方案在本项目里也实现了（{@link ElasticsearchRestSearchEngine}），
 *       两者接口一致，数据量超过 500 万或需要复杂相关度时切过去即可。</li>
 * </ol>
 *
 * <p><b>索引结构</b>：
 * <pre>
 *   term（词项） → PostingList（docId 数组 + 权重数组）
 *   docId        → Doc（输出与过滤所需的字段快照）
 *   docId        → docLength（用于 BM25 的长度归一化）
 * </pre>
 * 用"两个原生数组"而不是 {@code List<Posting>} 存倒排链，是为了避免每个倒排项
 * 产生一个对象头（200 万倒排项时，对象头开销就是几十 MB）。
 *
 * <p><b>BM25 打分公式</b>（k1=1.2, b=0.75 为业界默认经验值）：
 * <pre>
 *   idf(t)  = ln(1 + (N - df + 0.5) / (df + 0.5))
 *   score   = Σ idf(t) · (tf · (k1+1)) / (tf + k1 · (1 - b + b · dl / avgdl))
 * </pre>
 * 其中我们用一个"加权词频"代替原始 tf：{@code tf = Σ 字段权重 × 该词在该字段出现次数}，
 * 字段权重为 书名 3.0 / 标签 1.2 / 作者 1.5 / 分类 1.0 / 出版社 0.8。
 * 这是单字段 BM25 的多字段近似（真正的多字段 BM25 要为每个字段维护独立的长度归一化），
 * 用一次遍历换来实现复杂度与内存的大幅下降，实测相关度能满足书名检索场景。
 *
 * <p><b>一致性说明</b>：索引里的 {@code availableCopies} 来自建索引时的快照，
 * 后续通过 {@link #onStockChanged(long)} 增量修正，因此它<b>可能短暂偏旧</b>。
 * 所以检索结果里的"可借"只作为展示参考，需要精确库存时必须调库存接口。
 */
@Component
public class InvertedIndexSearchEngine implements SearchEngine, com.smartlib.service.StockService.StockChangeListener {

    public static final String NAME = "inverted-index";

    private static final Logger log = LoggerFactory.getLogger(InvertedIndexSearchEngine.class);

    /** BM25 参数。k1 控制词频饱和度，b 控制长度归一化强度。 */
    private static final double K1 = 1.2;
    private static final double B = 0.75;

    /** 字段权重。书名权重最高，因为用户最常按书名找书。 */
    private static final double W_TITLE = 3.0;
    private static final double W_TAGS = 1.2;
    private static final double W_AUTHOR = 1.5;
    private static final double W_CATEGORY = 1.0;
    private static final double W_PUBLISHER = 0.8;

    /** 整串命中书名的额外加分，用于把"精确短语"排在分词结果的更前面。 */
    private static final double PHRASE_BONUS = 8.0;

    /** 建索引时每批从数据库读取的行数，避免一次性加载全部书目。 */
    private static final int BUILD_BATCH_SIZE = 2000;

    private final BookMapper bookMapper;
    private final boolean buildOnStartup;
    private final int maxDocs;

    private volatile Index index = Index.empty();
    private final AtomicLong searchCount = new AtomicLong();
    private final AtomicLong totalSearchNanos = new AtomicLong();
    private final AtomicLong rebuildCount = new AtomicLong();

    public InvertedIndexSearchEngine(BookMapper bookMapper,
                                     @Value("${smartlib.search.index.build-on-startup:true}") boolean buildOnStartup,
                                     @Value("${smartlib.search.index.max-docs:500000}") int maxDocs) {
        this.bookMapper = bookMapper;
        this.buildOnStartup = buildOnStartup;
        this.maxDocs = maxDocs;
    }

    @PostConstruct
    public void init() {
        if (buildOnStartup) {
            rebuild();
        } else {
            log.warn("倒排索引未在启动时构建（smartlib.search.index.build-on-startup=false），"
                    + "检索将回退到数据库引擎，可在 /api/admin/search/reindex 手动触发构建");
        }
    }

    @Override
    public String name() {
        return NAME;
    }

    @Override
    public int indexedDocCount() {
        return index.docs.size();
    }

    @Override
    public boolean ready() {
        return !index.docs.isEmpty();
    }

    @Override
    public SearchResult search(BookQuery query) {
        long start = System.nanoTime();
        Index snapshot = this.index;
        if (snapshot.docs.isEmpty()) {
            return new SearchResult(List.of(), 0L, 0L, NAME, false, "索引为空，请先构建索引");
        }

        List<String> tokens = Tokenizer.tokenize(query.getQuery());
        if (tokens.isEmpty()) {
            return SearchResult.empty(NAME, 0L);
        }

        // 1. 累加每个候选文档的 BM25 得分
        Map<Long, Double> scores = new HashMap<>(1024);
        double avgLength = snapshot.avgLength <= 0 ? 1.0 : snapshot.avgLength;
        int docCount = snapshot.docs.size();
        for (String token : tokens) {
            PostingList postings = snapshot.postings.get(token);
            if (postings == null || postings.size == 0) {
                continue;
            }
            double idf = Math.log(1 + (docCount - postings.size + 0.5) / (postings.size + 0.5));
            for (int i = 0; i < postings.size; i++) {
                long docId = postings.docIds[i];
                double tf = postings.weights[i];
                Integer length = snapshot.docLengths.get(docId);
                double dl = length == null ? avgLength : length;
                double denominator = tf + K1 * (1 - B + B * dl / avgLength);
                double contribution = idf * (tf * (K1 + 1)) / denominator;
                scores.merge(docId, contribution, Double::sum);
            }
        }

        // 2. 过滤（分类、出版社、年份、仅看可借）。过滤放在打分之后做，
        //    因为过滤条件都作用在候选集上，不影响倒排链的读取效率。
        String queryLower = query.getQuery() == null ? "" : query.getQuery().toLowerCase();
        List<Scored> candidates = new ArrayList<>(scores.size());
        for (Map.Entry<Long, Double> entry : scores.entrySet()) {
            Doc doc = snapshot.docs.get(entry.getKey());
            if (doc == null || !matches(doc, query)) {
                continue;
            }
            double score = entry.getValue();
            // 书名包含完整查询串 → 加分，保证"计算机网络"这种精确输入排在最前
            if (!queryLower.isEmpty() && doc.titleLower().contains(queryLower)) {
                score += PHRASE_BONUS;
            }
            candidates.add(new Scored(doc, score));
        }

        // 3. 排序 + 分页
        candidates.sort(comparator(query.getSort()));
        long total = candidates.size();
        int from = Math.min(query.offset(), candidates.size());
        int to = Math.min(from + query.getSize(), candidates.size());
        List<BookView> items = candidates.subList(from, to).stream().map(s -> s.doc().toView()).toList();

        long tookMs = (System.nanoTime() - start) / 1_000_000;
        searchCount.incrementAndGet();
        totalSearchNanos.addAndGet(System.nanoTime() - start);
        return new SearchResult(items, total, tookMs, NAME, false, null);
    }

    private boolean matches(Doc doc, BookQuery query) {
        if (query.getCategory() != null && !query.getCategory().equals(doc.categoryCode)) {
            return false;
        }
        if (query.getPublisher() != null && (doc.publisher == null || !doc.publisher.contains(query.getPublisher()))) {
            return false;
        }
        if (query.getYearFrom() != null && (doc.publishYear == null || doc.publishYear < query.getYearFrom())) {
            return false;
        }
        if (query.getYearTo() != null && (doc.publishYear == null || doc.publishYear > query.getYearTo())) {
            return false;
        }
        return !Boolean.TRUE.equals(query.getAvailableOnly()) || doc.availableCopies > 0;
    }

    /**
     * 构造排序器。
     *
     * <p>刻意把每个比较器都赋给<b>显式声明类型的局部变量</b>，而不是在 switch 表达式里链式调用：
     * 在 switch 表达式的目标类型推断下，{@code Comparator.comparingLong((Scored s) -> ...)}
     * 的泛型参数会被推成 Object，导致 "cannot find symbol: doc" 这类看起来莫名其妙的编译错误。
     * 显式局部变量提供了明确的赋值上下文，问题消失，可读性也更好。
     */
    private Comparator<Scored> comparator(String sort) {
        Comparator<Scored> byScore = Comparator.comparingDouble((Scored s) -> s.score()).reversed();
        // 稳定排序的兜底：得分/年份/价格相同时按 id 升序，保证同一查询分页结果稳定不重复
        Comparator<Scored> tieBreak = Comparator.comparingLong(s -> s.doc().id());
        Comparator<Scored> byYearAsc = Comparator.comparingInt(
                (Scored s) -> s.doc().publishYear() == null ? 0 : s.doc().publishYear());
        Comparator<Scored> byYearDesc = byYearAsc.reversed();
        Comparator<Scored> byPriceAsc = Comparator.comparing(
                s -> s.doc().price() == null ? BigDecimal.ZERO : s.doc().price());
        Comparator<Scored> byPriceDesc = byPriceAsc.reversed();
        Comparator<Scored> byHot = Comparator.comparingLong((Scored s) -> s.doc().hotScore()).reversed();

        if (sort == null) {
            return byScore.thenComparing(tieBreak);
        }
        return switch (sort) {
            case "year_desc" -> byYearDesc.thenComparing(tieBreak);
            case "year_asc" -> byYearAsc.thenComparing(tieBreak);
            case "price_asc" -> byPriceAsc.thenComparing(tieBreak);
            case "price_desc" -> byPriceDesc.thenComparing(tieBreak);
            // 热门排序下，同热度用相关度、再同则用 id，保证结果确定
            case "hot" -> byHot.thenComparing(byScore).thenComparing(tieBreak);
            default -> byScore.thenComparing(tieBreak);
        };
    }

    /**
     * 全量重建索引。启动时调用，也可通过管理接口在数据批量变更后手动触发。
     *
     * <p>重建过程是"构建新索引 → 原子替换引用"，因此重建期间线上检索仍使用旧索引，
     * 不会出现"索引正在构建所以搜不到"的空窗期。
     */
    public synchronized IndexStats rebuild() {
        long start = System.nanoTime();
        Map<String, PostingList> postings = new HashMap<>(1 << 16);
        Map<Long, Doc> docs = new HashMap<>(1 << 15);
        Map<Long, Integer> docLengths = new HashMap<>(1 << 15);
        long totalLength = 0;
        int offset = 0;
        int loaded = 0;

        while (loaded < maxDocs) {
            List<BookRow> batch = bookMapper.selectPageForIndex(offset, BUILD_BATCH_SIZE);
            if (batch.isEmpty()) {
                break;
            }
            for (BookRow row : batch) {
                Doc doc = Doc.from(row);
                docs.put(doc.id, doc);
                int length = indexDoc(doc, postings);
                docLengths.put(doc.id, length);
                totalLength += length;
            }
            loaded += batch.size();
            offset += batch.size();
            if (batch.size() < BUILD_BATCH_SIZE) {
                break;
            }
        }

        double avgLength = docs.isEmpty() ? 1.0 : (double) totalLength / docs.size();
        this.index = new Index(postings, docs, docLengths, avgLength, (System.nanoTime() - start) / 1_000_000);
        rebuildCount.incrementAndGet();

        long postingCount = postings.values().stream().mapToLong(p -> p.size).sum();
        IndexStats stats = new IndexStats(docs.size(), postings.size(), postingCount,
                this.index.buildMillis, usedHeapBytes());
        log.info("倒排索引构建完成：{}", stats.describe());
        return stats;
    }

    /**
     * 把一个文档编入倒排表。
     *
     * <p>先在本文档内聚合词频（局部 map），再写入全局倒排链。
     * 这样保证同一个 (term, docId) 在倒排链里只出现一次，
     * 后续打分不必去重，也让数组结构可以直接使用。
     *
     * @return 该文档的加权长度（BM25 的长度归一化因子）
     */
    private int indexDoc(Doc doc, Map<String, PostingList> postings) {
        Map<String, Double> termWeights = new HashMap<>(32);
        accumulate(termWeights, doc.title, W_TITLE);
        accumulate(termWeights, doc.tags, W_TAGS);
        accumulate(termWeights, doc.author, W_AUTHOR);
        accumulate(termWeights, doc.categoryName, W_CATEGORY);
        accumulate(termWeights, doc.publisher, W_PUBLISHER);

        for (Map.Entry<String, Double> entry : termWeights.entrySet()) {
            postings.computeIfAbsent(entry.getKey(), k -> new PostingList()).add(doc.id, entry.getValue());
        }
        return termWeights.size();
    }

    private void accumulate(Map<String, Double> termWeights, String text, double weight) {
        if (text == null || text.isBlank()) {
            return;
        }
        for (String token : Tokenizer.tokenize(text)) {
            termWeights.merge(token, weight, Double::sum);
        }
    }

    /**
     * 库存变更时增量修正索引里的可借数（不重建倒排链，代价 O(1)）。
     * 这也是 {@code StockService.StockChangeListener} 的用途：
     * 保证"检索结果里显示的可借数"不会长期偏旧。
     */
    @Override
    public void onStockChanged(long bookId) {
        Index snapshot = this.index;
        Doc doc = snapshot.docs.get(bookId);
        if (doc == null) {
            return;
        }
        // 从数据库读取真实库存并替换快照。这里刻意不缓存库存，避免出现两个真相来源
        BookRow row = bookMapper.findRowsByIds(List.of(bookId)).stream().findFirst().orElse(null);
        if (row == null || row.getAvailableCopies() == null) {
            return;
        }
        snapshot.docs.put(bookId, doc.withAvailability(row.getAvailableCopies()));
    }

    /** 单条书目新增/修改后更新索引（采编上架场景）。 */
    public void upsert(long bookId) {
        Index snapshot = this.index;
        List<BookRow> rows = bookMapper.findRowsByIds(List.of(bookId));
        if (rows.isEmpty()) {
            snapshot.docs.remove(bookId);
            return;
        }
        Doc doc = Doc.from(rows.get(0));
        snapshot.docs.put(bookId, doc);
        // 注意：倒排链不在这里更新（需要先移除旧词项），因此新增书目要等下一次 rebuild 才能被检索到。
        // 这是刻意的取舍：上架是低频操作，重建索引成本远低于为它维护"可删除倒排链"的复杂度。
        log.info("书目 {} 已更新索引快照，倒排链将在下次重建时包含它", bookId);
    }

    public String describeIndex() {
        return index.describe();
    }

    public long getRebuildCount() {
        return rebuildCount.get();
    }

    public double avgSearchMillis() {
        long count = searchCount.get();
        return count == 0 ? 0 : (double) totalSearchNanos.get() / count / 1_000_000;
    }

    private static long usedHeapBytes() {
        Runtime runtime = Runtime.getRuntime();
        return runtime.totalMemory() - runtime.freeMemory();
    }

    /** 一次打分后的候选。 */
    private record Scored(Doc doc, double score) {
    }

    /** 索引统计，用于管理接口与压测报告。 */
    public record IndexStats(int docCount, int termCount, long postingCount, long buildMillis, long usedHeapBytes) {
        public String describe() {
            return String.format("文档=%d, 词项=%d, 倒排项=%d, 构建耗时=%dms, 当前堆占用=%.1fMB",
                    docCount, termCount, postingCount, buildMillis, usedHeapBytes / 1024.0 / 1024.0);
        }
    }

    /** 倒排链：两个原生数组，避免每个倒排项一个对象。 */
    static final class PostingList {
        private long[] docIds = new long[8];
        private double[] weights = new double[8];
        private int size;

        void add(long docId, double weight) {
            if (size == docIds.length) {
                int newCapacity = size + (size >> 1) + 8;
                long[] newIds = new long[newCapacity];
                double[] newWeights = new double[newCapacity];
                System.arraycopy(docIds, 0, newIds, 0, size);
                System.arraycopy(weights, 0, newWeights, 0, size);
                docIds = newIds;
                weights = newWeights;
            }
            docIds[size] = docId;
            weights[size] = weight;
            size++;
        }
    }

    /** 不可变文档快照。不可变是有意的：多线程检索时无需加锁。 */
    record Doc(long id, String isbn, String title, String titleLower, String author, String publisher,
               String categoryName, String categoryCode, Integer publishYear, BigDecimal price,
               String tags, String summary, String shelfLocation, int totalCopies, int availableCopies,
               long hotScore) {

        static Doc from(BookRow row) {
            String title = row.getTitle() == null ? "" : row.getTitle();
            return new Doc(
                    row.getId(),
                    row.getIsbn(),
                    title,
                    title.toLowerCase(),
                    row.getAuthor(),
                    row.getPublisher(),
                    row.getCategoryName(),
                    row.getCategoryCode(),
                    row.getPublishYear(),
                    row.getPrice(),
                    row.getTags(),
                    row.getSummary(),
                    row.getShelfLocation(),
                    row.getTotalCopies() == null ? 0 : row.getTotalCopies(),
                    row.getAvailableCopies() == null ? 0 : row.getAvailableCopies(),
                    row.getHotScore() == null ? 0L : row.getHotScore().longValue());
        }

        Doc withAvailability(int available) {
            return new Doc(id, isbn, title, titleLower, author, publisher, categoryName, categoryCode,
                    publishYear, price, tags, summary, shelfLocation, totalCopies, available, hotScore);
        }

        BookView toView() {
            return new BookView(id, isbn, title, author, publisher, categoryName, publishYear, price,
                    tags, summary, shelfLocation, totalCopies, availableCopies);
        }
    }

    /** 一份不可变的索引快照。 */
    static final class Index {
        final Map<String, PostingList> postings;
        final Map<Long, Doc> docs;
        final Map<Long, Integer> docLengths;
        final double avgLength;
        final long buildMillis;

        Index(Map<String, PostingList> postings, Map<Long, Doc> docs, Map<Long, Integer> docLengths,
              double avgLength, long buildMillis) {
            this.postings = postings;
            // 用 ConcurrentHashMap 包裹文档表：库存变更需要原地替换某个文档，而检索线程同时在读
            this.docs = new ConcurrentHashMap<>(docs);
            this.docLengths = docLengths;
            this.avgLength = avgLength;
            this.buildMillis = buildMillis;
        }

        static Index empty() {
            return new Index(new HashMap<>(), new HashMap<>(), new HashMap<>(), 1.0, 0L);
        }

        String describe() {
            return String.format("倒排索引[文档=%d, 词项=%d, 平均长度=%.1f, 构建耗时=%dms]",
                    docs.size(), postings.size(), avgLength, buildMillis);
        }
    }

    /** 暴露给管理接口的检索统计。 */
    public Map<String, Object> stats() {
        Map<String, Object> map = new HashMap<>();
        map.put("engine", NAME);
        map.put("docCount", index.docs.size());
        map.put("termCount", index.postings.size());
        map.put("avgLength", index.avgLength);
        map.put("buildMillis", index.buildMillis);
        map.put("rebuildCount", rebuildCount.get());
        map.put("searchCount", searchCount.get());
        map.put("avgSearchMillis", avgSearchMillis());
        map.put("maxDocs", maxDocs);
        map.put("builtAt", LocalDateTime.now().toString());
        return map;
    }
}
