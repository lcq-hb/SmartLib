package com.smartlib.web;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import com.smartlib.cache.BookIdBloomFilter;
import com.smartlib.cache.MultiLevelCache;
import com.smartlib.common.Result;
import com.smartlib.service.RuleEngine;
import com.smartlib.service.StockService;
import com.smartlib.service.search.InvertedIndexSearchEngine;
import com.smartlib.service.search.SearchEngineFactory;
import com.smartlib.service.stock.RedisLuaStockStrategy;
import com.smartlib.service.stock.StockStrategyFactory;
import com.smartlib.support.CircuitBreaker;
import com.smartlib.support.DegradeFallback;
import com.smartlib.support.TokenBucketRateLimiter;

/**
 * 管理接口：用于压测对比与线上排障。
 *
 * <p><b>安全设计</b>：整个控制器由 {@code smartlib.admin.enabled} 开关控制，
 * 本地/预发打开（压测需要它来切换策略），生产默认关闭。
 * 生产若要开放，必须放在内网并叠加鉴权 —— 这些接口能改库存策略、重建索引，权限极高。
 *
 * <p><b>它存在的价值</b>：没有这些接口，"六种库存策略的压测对比"就得改配置重启六次，
 * 每次重启都会让压测环境的状态不一致（缓存冷热不同、连接池重新预热），
 * 对比数据就不可信了。运行期切换保证六组数据在同一个进程、同一份数据上产出。
 */
@RestController
@RequestMapping("/api/admin")
@ConditionalOnProperty(name = "smartlib.admin.enabled", havingValue = "true")
public class AdminController {

    private final StockStrategyFactory stockStrategyFactory;
    private final SearchEngineFactory searchEngineFactory;
    private final InvertedIndexSearchEngine invertedIndexSearchEngine;
    private final StockService stockService;
    private final RuleEngine ruleEngine;
    private final MultiLevelCache multiLevelCache;
    private final BookIdBloomFilter bloomFilter;
    private final TokenBucketRateLimiter rateLimiter;
    private final CircuitBreaker circuitBreaker;
    private final DegradeFallback degradeFallback;
    private final List<RedisLuaStockStrategy> redisLuaStrategy;

    public AdminController(StockStrategyFactory stockStrategyFactory,
                           SearchEngineFactory searchEngineFactory,
                           InvertedIndexSearchEngine invertedIndexSearchEngine,
                           StockService stockService,
                           RuleEngine ruleEngine,
                           MultiLevelCache multiLevelCache,
                           BookIdBloomFilter bloomFilter,
                           TokenBucketRateLimiter rateLimiter,
                           CircuitBreaker circuitBreaker,
                           DegradeFallback degradeFallback,
                           List<RedisLuaStockStrategy> redisLuaStrategy) {
        this.stockStrategyFactory = stockStrategyFactory;
        this.searchEngineFactory = searchEngineFactory;
        this.invertedIndexSearchEngine = invertedIndexSearchEngine;
        this.stockService = stockService;
        this.ruleEngine = ruleEngine;
        this.multiLevelCache = multiLevelCache;
        this.bloomFilter = bloomFilter;
        this.rateLimiter = rateLimiter;
        this.circuitBreaker = circuitBreaker;
        this.degradeFallback = degradeFallback;
        this.redisLuaStrategy = redisLuaStrategy;
    }

    // ------------------------------ 库存策略 ------------------------------

    @GetMapping("/stock-strategy")
    public Result<Map<String, Object>> currentStockStrategy() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("current", stockStrategyFactory.currentName());
        data.put("available", stockStrategyFactory.availableNames());
        return Result.ok(data);
    }

    /**
     * 运行期切换库存扣减策略（压测用）。
     */
    @PutMapping("/stock-strategy")
    public Result<Map<String, Object>> switchStockStrategy(@RequestParam String name) {
        String previous = stockStrategyFactory.switchTo(name);
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("previous", previous);
        data.put("current", stockStrategyFactory.currentName());
        data.put("available", stockStrategyFactory.availableNames());
        return Result.ok(data);
    }

    /**
     * 库存对账：返回"库存与流水不守恒"或"出现负库存"的书目 id。
     * 正常情况下必须是空数组 —— 这是本项目"零超卖"的自动化证据。
     */
    @GetMapping("/reconcile")
    public Result<Map<String, Object>> reconcile() {
        List<Long> violations = stockService.reconcile();
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("violations", violations);
        data.put("violationCount", violations.size());
        data.put("consistent", violations.isEmpty());
        if (!redisLuaStrategy.isEmpty()) {
            data.put("redisLuaDegradedCount", redisLuaStrategy.get(0).degradedCount());
            data.put("redisLuaSuccessCount", redisLuaStrategy.get(0).luaSuccessCount());
        }
        return Result.ok(data);
    }

    // ------------------------------ 检索引擎 ------------------------------

    @GetMapping("/search-engine")
    public Result<Map<String, Object>> currentSearchEngine() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("current", searchEngineFactory.currentName());
        data.put("available", searchEngineFactory.availableNames());
        data.put("readiness", searchEngineFactory.readiness());
        return Result.ok(data);
    }

    @PutMapping("/search-engine")
    public Result<Map<String, Object>> switchSearchEngine(@RequestParam String name) {
        String previous = searchEngineFactory.switchTo(name);
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("previous", previous);
        data.put("current", searchEngineFactory.currentName());
        data.put("available", searchEngineFactory.availableNames());
        return Result.ok(data);
    }

    @GetMapping("/search/stats")
    public Result<Map<String, Object>> searchStats() {
        Map<String, Object> data = new LinkedHashMap<>(invertedIndexSearchEngine.stats());
        data.put("l1HitCount", multiLevelCache.l1HitCount());
        data.put("l2HitCount", multiLevelCache.l2HitCount());
        data.put("cacheMissCount", multiLevelCache.missCount());
        data.put("l1Size", multiLevelCache.l1Size());
        data.put("l2Name", multiLevelCache.l2Name());
        data.put("bloom", bloomFilter.describe());
        return Result.ok(data);
    }

    /** 手动重建倒排索引（批量导入书目后调用）。 */
    @PostMapping("/search/reindex")
    public Result<Map<String, Object>> reindex() {
        InvertedIndexSearchEngine.IndexStats stats = invertedIndexSearchEngine.rebuild();
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("docCount", stats.docCount());
        data.put("termCount", stats.termCount());
        data.put("postingCount", stats.postingCount());
        data.put("buildMillis", stats.buildMillis());
        data.put("usedHeapBytes", stats.usedHeapBytes());
        data.put("summary", stats.describe());
        return Result.ok(data);
    }

    // ------------------------------ 规则 ------------------------------

    @GetMapping("/rules")
    public Result<Map<String, String>> rules() {
        return Result.ok(ruleEngine.snapshot());
    }

    @PostMapping("/rules/reload")
    public Result<Integer> reloadRules() {
        return Result.ok(ruleEngine.refresh());
    }

    // ------------------------------ 韧性组件状态 ------------------------------

    @GetMapping("/resilience")
    public Result<Map<String, Object>> resilience() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("circuitBreakers", circuitBreaker.allStates());
        data.put("rateLimitRejected", rateLimiter.rejectedCount());
        data.put("rateLimitTrackedKeys", rateLimiter.trackedKeys());
        data.put("degradeCounters", degradeFallback.snapshot());
        return Result.ok(data);
    }
}
