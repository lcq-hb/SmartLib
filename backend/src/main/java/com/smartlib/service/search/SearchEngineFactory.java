package com.smartlib.service.search;

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
 * 检索引擎工厂：按配置选择当前引擎，配置错误时回退到数据库基线引擎。
 *
 * <p>回退而不是启动失败的原因：ES 集群没起来不应该让整个图书检索功能不可用。
 * 回退到 {@code like} 引擎虽然慢，但功能是完整的 —— "可用性优先于性能"。
 */
@Component
public class SearchEngineFactory {

    private static final Logger log = LoggerFactory.getLogger(SearchEngineFactory.class);

    private final Map<String, SearchEngine> engines = new LinkedHashMap<>();
    private volatile String currentName;

    public SearchEngineFactory(List<SearchEngine> discovered,
                               @Value("${smartlib.search.engine:inverted-index}") String configured) {
        for (SearchEngine engine : discovered) {
            engines.put(engine.name(), engine);
        }
        if (engines.isEmpty()) {
            throw new BizException(ErrorCode.SEARCH_ENGINE_ERROR, "没有发现任何检索引擎实现");
        }
        if (engines.containsKey(configured)) {
            this.currentName = configured;
        } else {
            this.currentName = LikeSearchEngine.NAME;
            log.warn("配置的检索引擎 [{}] 未注册（可能缺少对应 profile），已回退到 [{}]。可用引擎：{}",
                    configured, currentName, engines.keySet());
        }
        log.info("检索引擎已启用：{}，全部可用={}", currentName, engines.keySet());
    }

    public String currentName() {
        return currentName;
    }

    public SearchEngine current() {
        return engines.get(currentName);
    }

    public SearchEngine byName(String name) {
        SearchEngine engine = engines.get(name);
        if (engine == null) {
            throw new BizException(ErrorCode.SEARCH_ENGINE_ERROR, "未注册的检索引擎：" + name);
        }
        return engine;
    }

    public boolean contains(String name) {
        return engines.containsKey(name);
    }

    public Set<String> availableNames() {
        return engines.keySet();
    }

    /** 运行期切换引擎，供压测脚本对比 Q1~Q5。 */
    public synchronized String switchTo(String name) {
        SearchEngine engine = byName(name);
        String previous = this.currentName;
        this.currentName = engine.name();
        log.info("检索引擎切换：{} -> {}", previous, this.currentName);
        return previous;
    }

    /** 各引擎就绪状态，供 /actuator 与管理接口观测（例如 ES 未就绪就不该切过去）。 */
    public Map<String, Boolean> readiness() {
        Map<String, Boolean> result = new LinkedHashMap<>();
        engines.forEach((name, engine) -> result.put(name, engine.ready()));
        return result;
    }
}
