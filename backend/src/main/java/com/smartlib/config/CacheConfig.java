package com.smartlib.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import com.smartlib.cache.L2Cache;
import com.smartlib.cache.NoopL2Cache;

/**
 * 缓存相关装配。
 *
 * <p>当 Redis 的 L2 实现因配置被关闭时（{@code smartlib.cache.l2.enabled=false}，
 * 或本地测试环境），这里提供一个空实现兜底。
 * 这样 {@code MultiLevelCache} 永远能注入到一个 L2Cache，
 * 上层不需要写 {@code if (l2 != null)} 这种防御代码（见 ADR 关于"缓存可降级"的讨论）。
 */
@Configuration
public class CacheConfig {

    private static final Logger log = LoggerFactory.getLogger(CacheConfig.class);

    @Bean
    @ConditionalOnMissingBean(L2Cache.class)
    public L2Cache noopL2Cache() {
        log.info("未启用 Redis L2 缓存，使用 NoopL2Cache（多级缓存退化为进程内 L1）");
        return new NoopL2Cache();
    }
}
