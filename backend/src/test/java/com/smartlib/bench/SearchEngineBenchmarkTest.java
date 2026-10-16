package com.smartlib.bench;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;

import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.Dtos.BookView;
import com.smartlib.domain.dto.Dtos.SearchResult;
import com.smartlib.service.BookSearchService;
import com.smartlib.service.search.InvertedIndexSearchEngine;
import com.smartlib.service.search.SearchEngineFactory;

/**
 * 检索引擎对比基准（Q1 数据库 like 基线 vs Q3 自研倒排索引）。
 *
 * <p><b>这是"检索优化"这条叙事的证据来源。</b>测试会打印一张 Markdown 表格，
 * 包含两个引擎的 P50/P99 延迟、QPS 与召回率，可直接粘贴进
 * {@code docs/06-测试计划与报告.md} 与 {@code docs/09-多种解决方案对比手册.md}。
 *
 * <p><b>关于规模的诚实说明</b>：测试里造 2 万条书目（保证 CI 能在几十秒内跑完），
 * 而生产场景是 120 万条。延迟会随数据量增长，因此<b>本测试的绝对值不能直接当作生产指标</b>，
 * 它证明的是"两个引擎之间的相对差距"，以及"倒排索引的延迟随数据量增长远慢于全表扫描"。
 * 大规模数据由 {@code scripts/bench/search_bench.py} 单独压测。
 *
 * <p>关于召回率：两个引擎都是子串语义，理论上召回应当接近；
 * 它们的真正差距在延迟与相关度排序上。测试把两者都测出来并如实打印，
 * 不做"倒排索引召回更高"这种没有依据的宣称。
 */
@SpringBootTest
@ActiveProfiles("test")
class SearchEngineBenchmarkTest {

    private static final Logger log = LoggerFactory.getLogger(SearchEngineBenchmarkTest.class);

    private static final int BOOK_COUNT = 20_000;
    private static final int WARMUP_ROUNDS = 5;
    private static final int MEASURE_ROUNDS = 10;
    private static final int TOP_K = 10;

    private static final String[] CORES = {
            "计算机网络", "数据结构", "操作系统", "数据库系统", "编译原理",
            "软件工程", "人工智能", "机器学习", "线性代数", "概率论"
    };
    private static final String[] QUALIFIERS = {
            "基础", "进阶", "实践", "习题集", "原理", "应用", "导论", "实验", "教程", "案例"
    };

    @Autowired
    private JdbcTemplate jdbcTemplate;
    @Autowired
    private BookSearchService bookSearchService;
    @Autowired
    private SearchEngineFactory engineFactory;
    @Autowired
    private InvertedIndexSearchEngine invertedIndexSearchEngine;

    /**
     * 拼音首字母检索的端到端验证。
     *
     * <p><b>这个用例守护一个曾经真实存在的功能缺陷</b>：索引侧把拼音词项存为
     * {@code py:jsjwl}，而查询侧只生成了裸词项 {@code jsjwl}，两边永远匹配不上，
     * "用首字母搜中文书名"这个被写进文档的功能实际上完全不可用
     * （实测：{@code jsj} 与 {@code jsjwl} 都搜不到《计算机网络》）。
     *
     * <p>单元测试没能发现它，因为单元测试只验证了"索引侧会生成 py: 词项"，
     * 而没有验证"查询词和索引词能不能对上"。这类缺口只能靠端到端检索用例兜住 ——
     * 所以这个用例同时断言<b>完整首字母</b>与<b>首字母前缀</b>两种输入。
     */
    @Test
    @DisplayName("拼音首字母检索：jsjwl 与 jsj 都应命中《计算机网络》")
    void pinyinInitialsSearchFindsChineseBook() {
        // 该用例在种子数据上验证，不依赖 2 万条的基准数据
        invertedIndexSearchEngine.rebuild();

        for (String query : List.of("jsjwl", "jsj", "jsjw")) {
            BookQuery q = new BookQuery();
            q.setQuery(query);
            q.setSize(5);
            SearchResult result = invertedIndexSearchEngine.search(q.normalize());
            log.info("拼音检索 [{}] 命中 {} 条，Top1={}", query, result.total(),
                    result.items().isEmpty() ? "无" : result.items().get(0).title());
            assertThat(result.items())
                    .as("读者输入首字母 [%s] 应当能搜到中文书名，否则拼音检索功能不可用", query)
                    .isNotEmpty();
            assertThat(result.items().get(0).title())
                    .as("拼音首字母 [%s] 的 Top1 应为《计算机网络：自顶向下方法》", query)
                    .contains("计算机网络");
        }
    }

    @Test
    @DisplayName("检索基准：like 基线与自研倒排索引的延迟/召回对比")
    void compareSearchEngines() {
        // 1. 造数据（用 JdbcTemplate 批量插入：2 万条单条 insert 会慢到不可接受）
        List<String> titles = generateTitles(BOOK_COUNT);
        seedBooks(titles);
        log.info("已灌入测试书目 {} 条，当前库中总量 {} 条", titles.size(),
                jdbcTemplate.queryForObject("select count(1) from book", Long.class));

        // 2. 重建倒排索引（应用启动时索引为空库构建，新插入的数据必须重建才能被检索到）
        InvertedIndexSearchEngine.IndexStats stats = invertedIndexSearchEngine.rebuild();
        log.info("倒排索引构建结果：{}", stats.describe());

        // 3. 构造查询集与标注答案
        List<QueryCase> cases = buildQueryCases(titles);

        // 4. 分别压两个引擎
        Map<String, EngineMetrics> metrics = new LinkedHashMap<>();
        for (String engine : List.of("like", "inverted-index")) {
            engineFactory.switchTo(engine);
            metrics.put(engine, measure(engine, cases));
        }

        // 5. 打印 Markdown 表格（日志即报告，便于直接回填文档）
        printReport(stats, metrics, cases.size());
    }

    private List<String> generateTitles(int count) {
        List<String> titles = new ArrayList<>(count);
        for (int i = 0; i < count; i++) {
            String core = CORES[i % CORES.length];
            String qualifier = QUALIFIERS[(i / CORES.length) % QUALIFIERS.length];
            // 混入一部分不含核心词的书，制造"无关文档"，让召回率有真实意义
            if (i % 7 == 0) {
                titles.add("学术论文集第" + i + "卷");
            } else {
                titles.add(core + qualifier + "（第" + (i % 20 + 1) + "版）");
            }
        }
        return titles;
    }

    private void seedBooks(List<String> titles) {
        List<Object[]> batch = new ArrayList<>(titles.size());
        for (int i = 0; i < titles.size(); i++) {
            String title = titles.get(i);
            batch.add(new Object[]{
                    // isbn 列宽 varchar(20)，用 36 进制时间戳 + 序号保证唯一且不超长
                    "B" + Long.toString(System.nanoTime() + i, 36), title, "测试作者" + (i % 50), "测试出版社",
                    1L, 2000 + (i % 25), new java.math.BigDecimal(30 + (i % 70)),
                    CORES[i % CORES.length], "基准测试用书目", "三楼测试区", 5
            });
        }
        jdbcTemplate.batchUpdate("""
                insert into book(isbn, title, author, publisher, category_id, publish_year,
                                 price, tags, summary, shelf_location, total_copies, created_at, updated_at)
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), now())
                """, batch);
        // 库存表也要有对应记录，否则检索结果里的可借数是 0，无法验证"仅看可借"过滤
        jdbcTemplate.update("""
                insert into book_stock(book_id, total_copies, available_copies, locked_copies, version, updated_at)
                select b.id, 5, 5, 0, 0, now() from book b
                 where not exists (select 1 from book_stock s where s.book_id = b.id)
                """);
    }

    private List<QueryCase> buildQueryCases(List<String> titles) {
        List<QueryCase> cases = new ArrayList<>();
        // 全词查询：期望命中该核心词的全部书目
        for (String core : CORES) {
            Set<Long> expected = expectedIds(titles, core);
            cases.add(new QueryCase(core, expected));
        }
        // 部分词查询：模拟读者只记得书名片段（bigram 方案应该同样能召回）
        for (String partial : List.of("网络", "系统", "数据", "学习", "代数")) {
            Set<Long> expected = expectedIds(titles, partial);
            cases.add(new QueryCase(partial, expected));
        }
        return cases;
    }

    /**
     * 计算期望命中集合。
     *
     * <p>注意：这里用"标题包含查询串"作为标注口径，与两个引擎的子串语义一致，
     * 是一个客观可复现的标准（不是人工主观标注），因此召回率数字是可复核的。
     */
    private Set<Long> expectedIds(List<String> titles, String term) {
        // 数据库里的 id 从 1 开始自增，但种子数据已经占用了前若干 id，
        // 因此这里用"标题匹配"直接在数据库中查出期望 id，避免 id 推算错误
        return new LinkedHashSet<>(jdbcTemplate.queryForList(
                "select id from book where title like ?", Long.class, "%" + term + "%"));
    }

    private EngineMetrics measure(String engineName, List<QueryCase> cases) {
        List<Long> latencies = new ArrayList<>();
        double recallSum = 0;
        long totalStart = System.nanoTime();

        for (int round = 0; round < WARMUP_ROUNDS; round++) {
            for (QueryCase c : cases) {
                runQuery(c, 1);
            }
        }
        for (int round = 0; round < MEASURE_ROUNDS; round++) {
            for (QueryCase c : cases) {
                long start = System.nanoTime();
                SearchResult result = runQuery(c, TOP_K);
                latencies.add((System.nanoTime() - start) / 1_000_000);
                recallSum += recall(result, c.expected());
            }
        }
        long elapsedNanos = System.nanoTime() - totalStart;
        latencies.sort(Long::compareTo);
        int totalQueries = MEASURE_ROUNDS * cases.size();
        return new EngineMetrics(
                engineName,
                percentile(latencies, 50),
                percentile(latencies, 95),
                percentile(latencies, 99),
                latencies.get(latencies.size() - 1),
                totalQueries / (elapsedNanos / 1_000_000_000.0),
                recallSum / totalQueries,
                totalQueries);
    }

    private SearchResult runQuery(QueryCase c, int size) {
        BookQuery query = new BookQuery();
        query.setQuery(c.term());
        query.setSize(size);
        // 直接调用引擎，绕过多级缓存：基准测试要测真实计算开销，不能被测到缓存命中率
        return engineFactory.current().search(query.normalize());
    }

    private double recall(SearchResult result, Set<Long> expected) {
        if (expected.isEmpty()) {
            return 1.0;
        }
        int hit = 0;
        for (BookView view : result.items()) {
            if (expected.contains(view.id())) {
                hit++;
            }
        }
        int denominator = Math.min(TOP_K, expected.size());
        return (double) hit / denominator;
    }

    private long percentile(List<Long> sorted, int p) {
        if (sorted.isEmpty()) {
            return 0;
        }
        int index = (int) Math.ceil(p / 100.0 * sorted.size()) - 1;
        return sorted.get(Math.max(0, Math.min(index, sorted.size() - 1)));
    }

    private void printReport(InvertedIndexSearchEngine.IndexStats stats,
                             Map<String, EngineMetrics> metrics, int queryCount) {
        StringBuilder sb = new StringBuilder();
        sb.append("\n================ 检索引擎对比基准（真实运行结果） ================\n");
        sb.append("书目规模：").append(jdbcTemplate.queryForObject("select count(1) from book", Long.class))
                .append(" 条；查询数：").append(queryCount).append(" 个 × ")
                .append(MEASURE_ROUNDS).append(" 轮；TopK=").append(TOP_K).append("\n");
        sb.append("倒排索引：").append(stats.describe()).append("\n\n");
        sb.append("| 引擎 | P50(ms) | P95(ms) | P99(ms) | Max(ms) | QPS | 召回率@").append(TOP_K).append(" |\n");
        sb.append("|---|---|---|---|---|---|---|\n");
        for (EngineMetrics m : metrics.values()) {
            sb.append(String.format(Locale.ROOT,
                    "| %s | %d | %d | %d | %d | %.1f | %.2f%% |\n",
                    m.engine(), m.p50(), m.p95(), m.p99(), m.max(),
                    m.qps(), m.recall() * 100));
        }
        EngineMetrics like = metrics.get("like");
        EngineMetrics inverted = metrics.get("inverted-index");
        if (like != null && inverted != null && inverted.p99() > 0) {
            sb.append(String.format(Locale.ROOT,
                    "%n结论：自研倒排索引 P99 相对 like 基线提升 %.1f 倍；两者召回率相差 %.2f 个百分点。%n",
                    (double) like.p99() / inverted.p99(), Math.abs(like.recall() - inverted.recall()) * 100));
        }
        sb.append("================================================================\n");
        log.info(sb.toString());
    }

    private record QueryCase(String term, Set<Long> expected) {
    }

    private record EngineMetrics(String engine, long p50, long p95, long p99, long max,
                                 double qps, double recall, int totalQueries) {
    }
}
