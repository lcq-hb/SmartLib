package com.smartlib.service.stock;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;

/**
 * 库存策略工厂：按配置选择当前生效的扣减策略。
 *
 * <p>这是"六种方案"能被真正对比的关键：所有策略实现同一个接口，
 * 换方案只改配置（{@code smartlib.stock.strategy}）或调用 {@link #switchTo(String)}，
 * 业务代码一行不动，压测脚本可以用同一套请求把六种方案跑一遍。
 *
 * <p>容错策略：配置了不存在的策略名时<b>不启动失败</b>，而是回退到默认策略并打 WARN。
 * 理由：配置错误不应该让整个借阅服务不可用；但同时必须让运维看得见（WARN + 指标）。
 */
@Component
public class StockStrategyFactory {

    private static final Logger log = LoggerFactory.getLogger(StockStrategyFactory.class);

    private static final String DEFAULT_STRATEGY = OptimisticDbStockStrategy.NAME;

    private final Map<String, StockStrategy> strategies = new LinkedHashMap<>();
    private volatile String currentName;

    public StockStrategyFactory(List<StockStrategy> discovered,
                                @Value("${smartlib.stock.strategy:" + DEFAULT_STRATEGY + "}") String configuredName) {
        for (StockStrategy strategy : discovered) {
            strategies.put(strategy.name(), strategy);
        }
        if (strategies.isEmpty()) {
            throw new BizException(ErrorCode.STOCK_STRATEGY_UNKNOWN, "没有发现任何库存扣减策略实现");
        }
        if (!strategies.containsKey(configuredName)) {
            log.warn("配置的库存策略 [{}] 不存在，回退为默认策略 [{}]。可选值：{}",
                    configuredName, DEFAULT_STRATEGY, strategies.keySet());
            this.currentName = strategies.containsKey(DEFAULT_STRATEGY) ? DEFAULT_STRATEGY : strategies.keySet().iterator().next();
        } else {
            this.currentName = configuredName;
        }
        log.info("库存扣减策略已启用：{}，全部可选策略={}", this.currentName, strategies.keySet());
    }

    public StockStrategy current() {
        return strategies.get(currentName);
    }

    public String currentName() {
        return currentName;
    }

    public StockStrategy byName(String name) {
        StockStrategy strategy = strategies.get(name);
        if (strategy == null) {
            throw new BizException(ErrorCode.UNSUPPORTED_STRATEGY, "不支持的库存策略：" + name + "，可选：" + strategies.keySet());
        }
        return strategy;
    }

    public Set<String> availableNames() {
        return strategies.keySet();
    }

    /**
     * 运行期切换策略。压测脚本用它依次跑完六种方案，无需重启应用。
     * 切换是幂等的，重复切到同一策略不会产生副作用。
     */
    public synchronized String switchTo(String name) {
        byName(name);
        String previous = this.currentName;
        this.currentName = name;
        log.info("库存扣减策略切换：{} -> {}", previous, name);
        return previous;
    }
}
