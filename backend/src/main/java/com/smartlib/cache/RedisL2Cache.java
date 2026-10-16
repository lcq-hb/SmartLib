package com.smartlib.cache;

import java.time.Duration;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * 基于 Redis 的 L2 缓存实现。
 *
 * <p><b>内置短路（mini circuit breaker）</b>：Redis 挂掉时，如果每个请求都去尝试连接、
 * 等 500ms 超时再失败，那么"缓存故障"会迅速演变成"接口全部变慢"。
 * 因此这里维护一个连续失败计数，失败达到阈值后直接短路一段时间（默认 10 秒），
 * 期间所有请求都当作未命中 —— 这就是"缓存故障不影响可用性"的具体做法。
 *
 * <p>与 {@code RedisLuaStockStrategy} 的降级是两件事：
 * 这里是<b>读缓存</b>的降级（跳过缓存直接查库），
 * 那边是<b>扣减库存</b>的降级（换成数据库条件更新）。两者的共同点是都不会因为 Redis 故障而给出错误结果。
 */
@Component
@ConditionalOnProperty(name = "smartlib.search.cache.l2.enabled", havingValue = "true", matchIfMissing = true)
public class RedisL2Cache implements L2Cache {

    private static final Logger log = LoggerFactory.getLogger(RedisL2Cache.class);

    /** 连续失败多少次后短路。 */
    private static final int FAILURE_THRESHOLD = 3;
    /** 短路持续时间。 */
    private static final long SHORT_CIRCUIT_NANOS = Duration.ofSeconds(10).toNanos();

    private final StringRedisTemplate redis;
    private final MeterRegistry meterRegistry;

    private volatile int consecutiveFailures;
    private volatile long shortCircuitUntilNanos;

    public RedisL2Cache(StringRedisTemplate redis, MeterRegistry meterRegistry) {
        this.redis = redis;
        this.meterRegistry = meterRegistry;
    }

    @Override
    public String name() {
        return "redis";
    }

    @Override
    public boolean available() {
        return System.nanoTime() >= shortCircuitUntilNanos;
    }

    @Override
    public String get(String key) {
        if (!available()) {
            meterRegistry.counter("smartlib.cache.l2", "op", "get", "result", "short-circuit").increment();
            return null;
        }
        try {
            String value = redis.opsForValue().get(key);
            onSuccess("get", value != null ? "hit" : "miss");
            return value;
        } catch (Exception e) {
            onFailure("get", e);
            return null;
        }
    }

    @Override
    public void put(String key, String value, Duration ttl) {
        if (!available()) {
            return;
        }
        try {
            redis.opsForValue().set(key, value, ttl);
            onSuccess("put", "ok");
        } catch (Exception e) {
            onFailure("put", e);
        }
    }

    @Override
    public void evict(String key) {
        if (!available()) {
            return;
        }
        try {
            redis.delete(key);
            onSuccess("evict", "ok");
        } catch (Exception e) {
            onFailure("evict", e);
        }
    }

    private void onSuccess(String op, String result) {
        if (consecutiveFailures > 0) {
            log.info("Redis 连接恢复，退出短路状态");
        }
        consecutiveFailures = 0;
        meterRegistry.counter("smartlib.cache.l2", "op", op, "result", result).increment();
    }

    private void onFailure(String op, Exception e) {
        int failures = ++consecutiveFailures;
        meterRegistry.counter("smartlib.cache.l2", "op", op, "result", "error").increment();
        if (failures >= FAILURE_THRESHOLD) {
            shortCircuitUntilNanos = System.nanoTime() + SHORT_CIRCUIT_NANOS;
            log.warn("Redis 连续失败 {} 次，进入 {} 秒短路：{}", failures, SHORT_CIRCUIT_NANOS / 1_000_000_000L, e.toString());
        } else {
            log.debug("Redis {} 失败（第 {} 次）：{}", op, failures, e.toString());
        }
    }
}
