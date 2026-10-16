package com.smartlib.service.search;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import com.smartlib.common.Json;
import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.Dtos.BookView;
import com.smartlib.domain.dto.Dtos.SearchResult;

/**
 * Q5：Elasticsearch 引擎（可选，通过 {@code smartlib.search.engine=elasticsearch} 启用）。
 *
 * <p><b>为什么用 JDK HttpClient 直连 ES 的 REST 接口，而不是引入官方 Java Client</b>：
 * 官方 client 会带来一整套依赖（transport、sniffer、jakarta json 等）与版本绑定，
 * 而本项目只用到 {@code _search} 一个接口，HTTP + JSON 足够。
 * 真要做索引生命周期管理、批量写入、聚合分析时再引入官方 client 才是合理选择。
 *
 * <p><b>与其它引擎的关系</b>：它实现同一个 {@link SearchEngine} 接口，
 * 因此 {@code BookSearchService} 的缓存、熔断、降级逻辑对它一视同仁，
 * 切换引擎不需要改动任何一行业务代码 —— 这正是 SPI 的价值。
 *
 * <p><b>验证状态</b>：本机没有部署 Elasticsearch，因此本引擎<b>未经过实测</b>，
 * 只有接口契约与代码实现。这一点在评测报告与简历话术中必须如实说明，
 * 不能把"写了代码"说成"上线验证过"。
 */
@Component
@ConditionalOnProperty(name = "smartlib.search.engine", havingValue = "elasticsearch")
public class ElasticsearchRestSearchEngine implements SearchEngine {

    public static final String NAME = "elasticsearch";

    private static final Logger log = LoggerFactory.getLogger(ElasticsearchRestSearchEngine.class);

    private final String baseUrl;
    private final String indexName;
    private final HttpClient http;

    /** 上次健康探测结果，避免每个请求都去探活。 */
    private volatile boolean healthy = false;
    private volatile long lastHealthCheckNanos = 0;
    private static final long HEALTH_CHECK_INTERVAL_NANOS = Duration.ofSeconds(30).toNanos();

    public ElasticsearchRestSearchEngine(
            @Value("${smartlib.search.elasticsearch.base-url:http://localhost:9200}") String baseUrl,
            @Value("${smartlib.search.elasticsearch.index-name:smartlib_book}") String indexName) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        this.indexName = indexName;
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        log.info("Elasticsearch 检索引擎已启用：{}/{}（注意：需确保集群可访问）", this.baseUrl, indexName);
    }

    @Override
    public String name() {
        return NAME;
    }

    @Override
    public boolean ready() {
        long now = System.nanoTime();
        if (now - lastHealthCheckNanos < HEALTH_CHECK_INTERVAL_NANOS) {
            return healthy;
        }
        lastHealthCheckNanos = now;
        try {
            HttpResponse<String> resp = http.send(
                    HttpRequest.newBuilder(URI.create(baseUrl + "/_cluster/health"))
                            .timeout(Duration.ofSeconds(2)).GET().build(),
                    HttpResponse.BodyHandlers.ofString());
            healthy = resp.statusCode() == 200;
        } catch (Exception e) {
            healthy = false;
            log.warn("Elasticsearch 健康检查失败：{}", e.toString());
        }
        return healthy;
    }

    @Override
    public SearchResult search(BookQuery query) {
        long start = System.nanoTime();
        String body = buildQueryBody(query);
        try {
            HttpResponse<String> resp = http.send(
                    HttpRequest.newBuilder(URI.create(baseUrl + "/" + indexName + "/_search"))
                            .timeout(Duration.ofSeconds(3))
                            .header("Content-Type", "application/json")
                            .POST(HttpRequest.BodyPublishers.ofString(body))
                            .build(),
                    HttpResponse.BodyHandlers.ofString());

            if (resp.statusCode() != 200) {
                throw new IllegalStateException("ES 返回状态码 " + resp.statusCode() + ": " + resp.body());
            }
            long tookMs = (System.nanoTime() - start) / 1_000_000;
            return parseResponse(resp.body(), tookMs);
        } catch (Exception e) {
            // 抛出异常交给 BookSearchService 的熔断器处理并降级到数据库直查
            throw new IllegalStateException("Elasticsearch 检索失败: " + e.getMessage(), e);
        }
    }

    /**
     * 构造 multi_match 查询。
     * 字段权重与自研引擎保持一致（书名^3、标签^1.2、作者^1.5、分类^1、出版社^0.8），
     * 这样两个引擎的相关度排序才有可比性，切换时读者不会觉得"结果变差了"。
     */
    private String buildQueryBody(BookQuery query) {
        Map<String, Object> multiMatch = new LinkedHashMap<>();
        multiMatch.put("query", query.getQuery());
        multiMatch.put("fields", List.of("title^3", "tags^1.2", "author^1.5", "categoryName^1", "publisher^0.8"));
        multiMatch.put("type", "best_fields");

        List<Map<String, Object>> must = new ArrayList<>();
        must.add(Map.of("multi_match", multiMatch));
        List<Map<String, Object>> filter = new ArrayList<>();
        if (query.getCategory() != null) {
            filter.add(Map.of("term", Map.of("categoryCode", query.getCategory())));
        }
        if (query.getPublisher() != null) {
            filter.add(Map.of("match_phrase", Map.of("publisher", query.getPublisher())));
        }
        if (query.getYearFrom() != null || query.getYearTo() != null) {
            Map<String, Object> range = new LinkedHashMap<>();
            if (query.getYearFrom() != null) {
                range.put("gte", query.getYearFrom());
            }
            if (query.getYearTo() != null) {
                range.put("lte", query.getYearTo());
            }
            filter.add(Map.of("range", Map.of("publishYear", range)));
        }
        if (Boolean.TRUE.equals(query.getAvailableOnly())) {
            filter.add(Map.of("range", Map.of("availableCopies", Map.of("gt", 0))));
        }

        Map<String, Object> bool = new LinkedHashMap<>();
        bool.put("must", must);
        if (!filter.isEmpty()) {
            bool.put("filter", filter);
        }

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("query", Map.of("bool", bool));
        body.put("from", query.offset());
        body.put("size", query.getSize());
        body.put("track_total_hits", true);
        return Json.toJson(body);
    }

    /**
     * 解析 ES 响应。
     *
     * <p>刻意用 {@code Map} 而不是 JsonNode：Jackson 3 的 JsonNode 方法名有变动，
     * 用 Map 能让这段代码不依赖具体 Jackson 版本的节点 API。
     */
    @SuppressWarnings("unchecked")
    private SearchResult parseResponse(String body, long tookMs) {
        Map<String, Object> root = Json.fromJson(body, Map.class);
        Map<String, Object> hitsWrapper = (Map<String, Object>) root.get("hits");
        if (hitsWrapper == null) {
            return SearchResult.empty(NAME, tookMs);
        }
        long total = 0;
        Object totalNode = hitsWrapper.get("total");
        if (totalNode instanceof Map<?, ?> totalMap) {
            total = ((Number) totalMap.get("value")).longValue();
        } else if (totalNode instanceof Number number) {
            total = number.longValue();
        }

        List<BookView> items = new ArrayList<>();
        Object hitsNode = hitsWrapper.get("hits");
        if (hitsNode instanceof List<?> hitList) {
            for (Object hit : hitList) {
                Map<String, Object> source = (Map<String, Object>) ((Map<String, Object>) hit).get("_source");
                if (source == null) {
                    continue;
                }
                items.add(new BookView(
                        asLong(source.get("id")),
                        asString(source.get("isbn")),
                        asString(source.get("title")),
                        asString(source.get("author")),
                        asString(source.get("publisher")),
                        asString(source.get("categoryName")),
                        source.get("publishYear") == null ? null : ((Number) source.get("publishYear")).intValue(),
                        source.get("price") == null ? null : new java.math.BigDecimal(source.get("price").toString()),
                        asString(source.get("tags")),
                        asString(source.get("summary")),
                        asString(source.get("shelfLocation")),
                        source.get("totalCopies") == null ? 0 : ((Number) source.get("totalCopies")).intValue(),
                        source.get("availableCopies") == null ? 0 : ((Number) source.get("availableCopies")).intValue()));
            }
        }
        return new SearchResult(items, total, tookMs, NAME, false, null);
    }

    private static Long asLong(Object value) {
        return value == null ? null : ((Number) value).longValue();
    }

    private static String asString(Object value) {
        return value == null ? null : value.toString();
    }
}
