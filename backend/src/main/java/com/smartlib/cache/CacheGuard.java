package com.smartlib.cache;

import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import com.smartlib.common.Json;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * 缓存护栏：解决缓存三大经典问题里的两个（击穿、穿透）。
 *
 * <p><b>1. 缓存击穿 —— SingleFlight（单飞）</b><br>
 * 热点 key 失效的瞬间，可能有几百个请求同时发现缓存没了，然后一起去查数据库。
 * 解决思路是"同一时刻同一个 key 只放行一个请求去加载"，其余请求等待并共享结果。
 * 这里用 {@code ConcurrentHashMap<key, CompletableFuture>} 实现：
 * {@code computeIfAbsent} 保证只有一个线程能创建 Future，其余线程拿到的是同一个 Future。
 * 注意 {@code CompletableFuture} 完成了要从 map 里移除，否则内存会一直涨。
 *
 * <p><b>2. 缓存穿透 —— 空值缓存</b><br>
 * 查询一个"数据库里也不存在"的 id 时，缓存永远不会命中（因为没有值可缓存），
 * 每次请求都会打到数据库。解决办法是把"空结果"也缓存起来，但要给一个很短的 TTL
 * （这里 60 秒），因为它随时可能变成"有值"（比如书刚上架）。
 * 更彻底的方案是前置布隆过滤器（见 {@link BloomFilter}），
 * 本项目是<b>两者组合</b>：布隆做概率拦截，空值缓存做精确兜底。
 *
 * <p>两个问题都会真实地打垮数据库，因此这里不是"优化"，而是必需项。
 */
@Component
public class CacheGuard {

    private static final Logger log = LoggerFactory.getLogger(CacheGuard.class);

    /** 空结果的占位符。用它而不是 null，是因为缓存里"存了 null"和"没存过"必须能区分。 */
    private static final String NULL_SENTINEL = "__NULL__";

    /** 空结果缓存时长：短于正常 TTL，因为"不存在"随时可能变成"存在"。 */
    private static final Duration NULL_TTL = Duration.ofSeconds(60);

    private final MultiLevelCache cache;
    private final MeterRegistry meterRegistry;
    private final ConcurrentHashMap<String, CompletableFuture<String>> inFlight = new ConcurrentHashMap<>();
    private final AtomicLong singleFlightJoined = new AtomicLong();

    public CacheGuard(MultiLevelCache cache, MeterRegistry meterRegistry) {
        this.cache = cache;
        this.meterRegistry = meterRegistry;
    }

    /**
     * 读缓存，未命中则由 loader 加载并回填。
     *
     * @param key    缓存 key
     * @param ttl    正常 TTL
     * @param type   返回类型（必须是 Jackson 能反序列化的具体类）
     * @param loader 回源逻辑；返回 null 表示"确实不存在"，会被当作空值缓存
     */
    public <T> T get(String key, Duration ttl, Class<T> type, Supplier<T> loader) {
        String cached = cache.get(key);
        if (NULL_SENTINEL.equals(cached)) {
            meterRegistry.counter("smartlib.cache.guard", "result", "null-hit").increment();
            return null;
        }
        if (cached != null) {
            return deserialize(cached, type);
        }

        // 单飞：只有第一个请求真正执行 loader，其余请求等待同一个 Future
        CompletableFuture<String> future = inFlight.computeIfAbsent(key, k -> {
            CompletableFuture<String> created = new CompletableFuture<>();
            try {
                String loaded = load(key, ttl, loader);
                created.complete(loaded == null ? NULL_SENTINEL : loaded);
            } catch (Exception e) {
                created.completeExceptionally(e);
            }
            return created;
        });

        // 如果这次请求不是"创建者"，说明它搭了别人的车
        if (inFlight.get(key) == future && future.isDone()) {
            singleFlightJoined.incrementAndGet();
        }

        try {
            String value = future.get();
            return NULL_SENTINEL.equals(value) ? null : deserialize(value, type);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            // 被中断时退化为直接回源，不能因为等待兄弟请求而影响主流程
            return loader.get();
        } catch (ExecutionException e) {
            log.warn("缓存加载失败，退化为直接回源 key={}", key, e.getCause());
            return loader.get();
        } finally {
            inFlight.remove(key, future);
        }
    }

    private <T> String load(String key, Duration ttl, Supplier<T> loader) {
        T value = loader.get();
        if (value == null) {
            // 空值缓存：把"不存在"也记下来，防止同一个不存在的 id 被反复穿透
            cache.put(key, NULL_SENTINEL, NULL_TTL);
            log.debug("缓存空值 key={} ttl={}s", key, NULL_TTL.toSeconds());
            return null;
        }
        String json = Json.toJson(value);
        cache.put(key, json, ttl);
        return json;
    }

    private <T> T deserialize(String json, Class<T> type) {
        try {
            return Json.fromJson(json, type);
        } catch (Exception e) {
            // 缓存里的旧格式/脏数据不应该让接口报错，当作未命中处理即可
            log.warn("缓存反序列化失败，按未命中处理 type={} err={}", type.getSimpleName(), e.toString());
            return null;
        }
    }

    public void evict(String key) {
        cache.evict(key);
    }

    /** 正在加载中的 key 数量，正常应接近 0；持续很高说明后端回源很慢。 */
    public int inFlightCount() {
        return inFlight.size();
    }

    public long singleFlightJoinedCount() {
        return singleFlightJoined.get();
    }
}
