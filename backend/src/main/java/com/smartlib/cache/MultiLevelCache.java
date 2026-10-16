package com.smartlib.cache;

import java.time.Duration;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicLong;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import com.github.benmanes.caffeine.cache.Cache;
import com.github.benmanes.caffeine.cache.Caffeine;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * 多级缓存：Caffeine（L1 进程内）+ Redis（L2 分布式）。
 *
 * <p><b>为什么要两级</b>：
 * <ul>
 *   <li>只放 Redis：每次读缓存都要走网络，一次典型往返 0.5~1ms。检索接口 P99 目标 300ms，
 *       一个请求可能读十几次缓存，网络开销就吃掉大半预算；</li>
 *   <li>只放进程内：多实例部署下各实例缓存不一致，且缓存容量受堆内存限制无法共享；</li>
 *   <li>两级组合：L1 命中率通常在 80% 以上（热点集中），L1 未命中再走 L2，
 *       把网络访问量压到原来的两成。</li>
 * </ul>
 *
 * <p><b>三级缓存体系里的第三级是数据库</b>，由 {@link CacheGuard} 负责回填。
 *
 * <p><b>TTL 随机抖动防雪崩</b>：如果一批热点 key 在同一时刻写入、又设了相同的 TTL，
 * 它们会在同一时刻同时失效，所有请求一起压到数据库（雪崩）。
 * 这里给每个 key 的 TTL 加上 ±ratio 的随机偏移，把失效时刻打散。
 */
@Component
public class MultiLevelCache {

    private static final Logger log = LoggerFactory.getLogger(MultiLevelCache.class);

    private final Cache<String, String> l1;
    private final L2Cache l2;
    private final double ttlJitterRatio;
    private final MeterRegistry meterRegistry;
    private final AtomicLong l1Hit = new AtomicLong();
    private final AtomicLong l2Hit = new AtomicLong();
    private final AtomicLong miss = new AtomicLong();

    public MultiLevelCache(
            L2Cache l2,
            MeterRegistry meterRegistry,
            @Value("${smartlib.search.cache.l1-max-size:10000}") long l1MaxSize,
            @Value("${smartlib.search.cache.l1-ttl-seconds:300}") long l1TtlSeconds,
            @Value("${smartlib.search.cache.ttl-jitter-ratio:0.1}") double ttlJitterRatio) {
        this.l2 = l2;
        this.meterRegistry = meterRegistry;
        this.ttlJitterRatio = ttlJitterRatio;
        // maximumSize 是必须的：没有容量上限的进程内缓存，在检索词无限多样的场景下等价于内存泄漏
        this.l1 = Caffeine.newBuilder()
                .maximumSize(l1MaxSize)
                .expireAfterWrite(Duration.ofSeconds(l1TtlSeconds))
                .recordStats()
                .build();
        log.info("多级缓存初始化完成：L1=Caffeine(maxSize={}, ttl={}s), L2={}, TTL 抖动=±{}%",
                l1MaxSize, l1TtlSeconds, l2.name(), (int) (ttlJitterRatio * 100));
    }

    /**
     * 依次查 L1、L2。
     *
     * @return 命中返回值；两级都没命中返回 null
     */
    public String get(String key) {
        String value = l1.getIfPresent(key);
        if (value != null) {
            count("l1-hit");
            return value;
        }
        value = l2.get(key);
        if (value != null) {
            count("l2-hit");
            // 回填 L1：下次同实例请求就不必再走网络
            l1.put(key, value);
            return value;
        }
        count("miss");
        return null;
    }

    public void put(String key, String value, Duration ttl) {
        l1.put(key, value);
        l2.put(key, value, jitter(ttl));
    }

    /** 只写 L1。用于"降级期间仍然希望本实例快速响应"的场景（如 ES 不可用时的 DB 直查结果）。 */
    public void putLocalOnly(String key, String value) {
        l1.put(key, value);
    }

    public void evict(String key) {
        l1.invalidate(key);
        l2.evict(key);
    }

    public void clearLocal() {
        l1.invalidateAll();
    }

    /**
     * 给 TTL 加上 ±ratio 的随机抖动，防止同一批 key 同时失效造成缓存雪崩。
     */
    Duration jitter(Duration ttl) {
        if (ttlJitterRatio <= 0) {
            return ttl;
        }
        long millis = ttl.toMillis();
        long delta = (long) (millis * ttlJitterRatio);
        if (delta <= 0) {
            return ttl;
        }
        long jittered = millis + ThreadLocalRandom.current().nextLong(-delta, delta + 1);
        return Duration.ofMillis(Math.max(1, jittered));
    }

    public double l1HitRate() {
        return l1.stats().hitRate();
    }

    public long l1HitCount() {
        return l1Hit.get();
    }

    public long l2HitCount() {
        return l2Hit.get();
    }

    public long missCount() {
        return miss.get();
    }

    public long l1Size() {
        return l1.estimatedSize();
    }

    public String l2Name() {
        return l2.name();
    }

    private void count(String result) {
        switch (result) {
            case "l1-hit" -> l1Hit.incrementAndGet();
            case "l2-hit" -> l2Hit.incrementAndGet();
            default -> miss.incrementAndGet();
        }
        meterRegistry.counter("smartlib.cache.level", "result", result).increment();
    }
}
