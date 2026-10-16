package com.smartlib.cache;

import java.time.Duration;

/**
 * 无 L2 缓存的实现（本地开发 / 单元测试 / Redis 被整体摘除时使用）。
 *
 * <p>它是"降级"这个设计能成立的关键：如果上层直接依赖 RedisTemplate，
 * 那么"没有 Redis"就变成了一个必须到处判断的特殊情况；
 * 有了这个空实现，"没有 L2"就是一次正常的策略替换。
 */
public class NoopL2Cache implements L2Cache {

    @Override
    public String get(String key) {
        return null;
    }

    @Override
    public void put(String key, String value, Duration ttl) {
        // 故意不做任何事
    }

    @Override
    public void evict(String key) {
        // 故意不做任何事
    }

    @Override
    public boolean available() {
        return false;
    }

    @Override
    public String name() {
        return "noop";
    }
}
