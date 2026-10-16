package com.smartlib.cache;

import java.time.Duration;

/**
 * L2 分布式缓存抽象。
 *
 * <p>为什么要有这层接口而不是直接注入 {@code RedisTemplate}：
 * <ol>
 *   <li><b>可降级</b>：本地开发、单元测试没有 Redis，实现换成 {@link NoopL2Cache}即可，
 *       上层代码完全不感知，也不需要 {@code if (redis != null)} 到处判断；</li>
 *   <li><b>可替换</b>：换成 Memcached、Tair 或云厂商缓存只改一个实现类；</li>
 *   <li><b>可测试</b>：单测里注入一个内存实现的 L2，就能验证"L1 未命中 → L2 命中"的路径，
 *       而不需要真的起一个 Redis。</li>
 * </ol>
 *
 * <p>所有实现都必须遵守：<b>任何异常都不能向上抛</b>。缓存是加速层，
 * 缓存故障最多导致性能下降，绝不能让业务不可用。
 */
public interface L2Cache {

    /** @return 值；未命中或缓存不可用时返回 null */
    String get(String key);

    void put(String key, String value, Duration ttl);

    void evict(String key);

    /** L2 当前是否可用（用于监控与决定是否回填）。 */
    boolean available();

    /** 实现名，用于日志与指标标签。 */
    String name();
}
