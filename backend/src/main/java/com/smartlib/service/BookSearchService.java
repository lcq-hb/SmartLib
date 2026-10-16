package com.smartlib.service;

import java.time.Duration;
import java.util.Locale;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import com.smartlib.cache.CacheGuard;
import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.Dtos.SearchResult;
import com.smartlib.service.search.LikeSearchEngine;
import com.smartlib.service.search.SearchEngine;
import com.smartlib.service.search.SearchEngineFactory;
import com.smartlib.support.CircuitBreaker;
import com.smartlib.support.DegradeFallback;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * 书目检索服务：把"引擎选择 + 多级缓存 + 熔断降级 + 指标"这几层串起来。
 *
 * <p><b>请求分流规则（很重要的一条实测结论）</b>：
 * <ul>
 *   <li><b>有关键词</b> → 走全文检索引擎（默认自研倒排索引）；</li>
 *   <li><b>无关键词</b>（纯浏览：按分类/年份/仅看可借翻页）→ 直接走数据库引擎。
 *       这类请求本身就能命中 B+ 树索引，走倒排索引反而要先把全部文档捞出来再内存过滤，
 *       是"为了用索引而用索引"的反模式。</li>
 * </ul>
 *
 * <p><b>降级链</b>：当前引擎异常 → 熔断器打开 → 降级到 {@code like} 引擎（功能完整但慢）
 * → 若数据库也异常 → 返回带 {@code degraded=true} 的空结果并说明原因。
 * 任何一步都不会向调用方抛异常，因为"检索失败"不应该让整个页面报错。
 */
@Service
public class BookSearchService {

    private static final Logger log = LoggerFactory.getLogger(BookSearchService.class);

    private final SearchEngineFactory engineFactory;
    private final CacheGuard cacheGuard;
    private final CircuitBreaker circuitBreaker;
    private final DegradeFallback degradeFallback;
    private final MeterRegistry meterRegistry;
    private final Duration cacheTtl;

    public BookSearchService(SearchEngineFactory engineFactory,
                             CacheGuard cacheGuard,
                             CircuitBreaker circuitBreaker,
                             DegradeFallback degradeFallback,
                             MeterRegistry meterRegistry,
                             @Value("${smartlib.search.cache.l2-ttl-seconds:1800}") long cacheTtlSeconds) {
        this.engineFactory = engineFactory;
        this.cacheGuard = cacheGuard;
        this.circuitBreaker = circuitBreaker;
        this.degradeFallback = degradeFallback;
        this.meterRegistry = meterRegistry;
        this.cacheTtl = Duration.ofSeconds(cacheTtlSeconds);
    }

    public SearchResult search(BookQuery query) {
        query.normalize();
        // 无关键词的浏览型请求不值得用倒排索引，见类注释的分流规则
        String engineName = query.hasQuery() ? engineFactory.currentName() : LikeSearchEngine.NAME;
        String cacheKey = cacheKey(query, engineName);

        SearchResult result = cacheGuard.get(cacheKey, cacheTtl, SearchResult.class, () -> doSearch(query, engineName));
        if (result == null) {
            return SearchResult.empty(engineName, 0L);
        }
        return result;
    }

    private SearchResult doSearch(BookQuery query, String engineName) {
        SearchEngine engine = engineFactory.byName(engineName);
        SearchResult result = circuitBreaker.call(
                "search-" + engineName,
                () -> engine.search(query),
                () -> fallbackSearch(query, engineName));
        meterRegistry.timer("smartlib.search.latency", "engine", engineName).record(result.tookMs(), java.util.concurrent.TimeUnit.MILLISECONDS);
        meterRegistry.counter("smartlib.search.requests", "engine", engineName,
                "degraded", String.valueOf(result.degraded())).increment();
        return result;
    }

    /**
     * 降级路径：先尝试数据库直查，再退到"返回空结果 + 说明原因"。
     * 注意降级结果里带 {@code degradeReason}，前端与 Agent 可以据此提示"当前为简化结果"。
     */
    private SearchResult fallbackSearch(BookQuery query, String failedEngine) {
        String reason = "检索引擎[" + failedEngine + "]不可用，已降级为数据库直查";
        log.warn("检索降级：{}", reason);
        try {
            SearchEngine like = engineFactory.byName(LikeSearchEngine.NAME);
            SearchResult result = like.search(query);
            return new SearchResult(result.items(), result.total(), result.tookMs(),
                    like.name(), true, reason);
        } catch (Exception e) {
            log.error("降级检索同样失败，返回空结果", e);
            return degradeFallback.search("检索引擎与数据库均不可用");
        }
    }

    /**
     * 缓存 key。把所有影响结果的参数都编进去，否则会出现"换了个筛选条件却拿到上一个条件的结果"。
     */
    private String cacheKey(BookQuery query, String engineName) {
        return String.format(Locale.ROOT, "smartlib:search:%s:%s|%s|%s|%s|%s|%s|%s|%d|%d",
                engineName,
                nullSafe(query.getQuery()),
                nullSafe(query.getCategory()),
                nullSafe(query.getPublisher()),
                query.getYearFrom(),
                query.getYearTo(),
                query.getAvailableOnly(),
                nullSafe(query.getSort()),
                query.getPage(),
                query.getSize());
    }

    private String nullSafe(String value) {
        return value == null ? "" : value;
    }
}
