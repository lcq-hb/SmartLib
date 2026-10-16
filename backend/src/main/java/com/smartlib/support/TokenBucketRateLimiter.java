package com.smartlib.support;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 令牌桶限流器（按 key 维度）。
 *
 * <p>为什么自己写而不是直接上 Sentinel：
 * <ol>
 *   <li>本项目要限流的入口只有"公共 API"与"内部工具 API"两类，
 *       需求是"按调用方维度限速"，令牌桶 40 行就能覆盖，引入 Sentinel 会带来
 *       Dashboard、规则推送、持久化规则源一整套运维面；</li>
 *   <li>限流是面试高频考点，自己实现能把"桶容量 / 补充速率 / 突发流量"三者的关系讲清楚；</li>
 *   <li>一旦需要集群级限流、热点参数限流、系统自适应保护，再换 Sentinel 是完全正当的决策
 *       —— 这个取舍写在 ADR-005 里。</li>
 * </ol>
 *
 * <p><b>算法要点：</b>不做定时补充（那样需要一个后台线程扫描所有 key），
 * 而是"惰性补充"——每次取令牌时按经过的时间算出应该补充多少。
 * 这是令牌桶最常见的工程实现，零后台线程、零定时器。
 *
 * <p><b>它保护不了什么：</b>限流器状态在单个 JVM 内，多实例部署下每个实例各有各的桶，
 * 集群总速率是实例数倍。要做到集群精确限流必须把桶放进 Redis（见部署手册限流章节）。
 */
@Component
public class TokenBucketRateLimiter {

    private static final Logger log = LoggerFactory.getLogger(TokenBucketRateLimiter.class);

    /** 空闲桶的存活时间：超过这个时间没有访问就被清理，避免 key 无限增长（内存泄漏防护）。 */
    private static final long IDLE_EVICT_NANOS = 10 * 60 * 1_000_000_000L;

    private final boolean enabled;
    private final double capacity;
    private final double refillPerSecond;
    private final Map<String, Bucket> buckets = new ConcurrentHashMap<>();
    private final AtomicLong rejected = new AtomicLong();
    private final AtomicLong cleanedBuckets = new AtomicLong();

    public TokenBucketRateLimiter(
            @Value("${smartlib.rate-limit.enabled:true}") boolean enabled,
            @Value("${smartlib.rate-limit.capacity:400}") double capacity,
            @Value("${smartlib.rate-limit.refill-per-second:400}") double refillPerSecond) {
        this.enabled = enabled;
        this.capacity = capacity;
        this.refillPerSecond = refillPerSecond;
    }

    /**
     * 尝试获取一个令牌。
     *
     * @param key 限流维度，例如 {@code api:books} 或 {@code internal:reader:1001}
     * @return true 表示放行，false 表示应返回 429
     */
    public boolean tryAcquire(String key) {
        if (!enabled) {
            return true;
        }
        Bucket bucket = buckets.computeIfAbsent(key, k -> new Bucket(capacity));
        boolean allowed = bucket.tryAcquire(1, refillPerSecond, capacity);
        if (!allowed) {
            rejected.incrementAndGet();
        }
        return allowed;
    }

    /** 当前剩余令牌数，用于 /actuator 指标与压测观测。 */
    public double availableTokens(String key) {
        Bucket bucket = buckets.get(key);
        return bucket == null ? capacity : bucket.availableTokens(refillPerSecond, capacity);
    }

    public long rejectedCount() {
        return rejected.get();
    }

    public int trackedKeys() {
        return buckets.size();
    }

    /**
     * 清理长期空闲的桶。只在有访问时触发一次抽样清理，避免额外的定时任务。
     */
    public void evictIdle() {
        long now = System.nanoTime();
        int before = buckets.size();
        buckets.entrySet().removeIf(e -> now - e.getValue().lastAccessNanos > IDLE_EVICT_NANOS);
        int removed = before - buckets.size();
        if (removed > 0) {
            cleanedBuckets.addAndGet(removed);
            log.debug("清理空闲限流桶 {} 个，当前 {} 个", removed, buckets.size());
        }
    }

    public long cleanedBucketCount() {
        return cleanedBuckets.get();
    }

    /**
     * 单个令牌桶。用 synchronized 而不是 CAS 循环：
     * 临界区只有几行算术，锁竞争时间以纳秒计，可读性远好于一条难懂的 CAS 重试循环。
     */
    private static final class Bucket {

        private double tokens;
        private long lastRefillNanos;
        private volatile long lastAccessNanos;

        Bucket(double capacity) {
            this.tokens = capacity;
            this.lastRefillNanos = System.nanoTime();
            this.lastAccessNanos = this.lastRefillNanos;
        }

        synchronized boolean tryAcquire(int permits, double refillPerSecond, double capacity) {
            refill(refillPerSecond, capacity);
            lastAccessNanos = System.nanoTime();
            if (tokens >= permits) {
                tokens -= permits;
                return true;
            }
            return false;
        }

        synchronized double availableTokens(double refillPerSecond, double capacity) {
            refill(refillPerSecond, capacity);
            return tokens;
        }

        private void refill(double refillPerSecond, double capacity) {
            long now = System.nanoTime();
            double elapsedSeconds = (now - lastRefillNanos) / 1_000_000_000.0;
            if (elapsedSeconds <= 0) {
                return;
            }
            // 惰性补充：按流逝时间折算令牌，并且不允许超过桶容量
            tokens = Math.min(capacity, tokens + elapsedSeconds * refillPerSecond);
            lastRefillNanos = now;
        }
    }
}
