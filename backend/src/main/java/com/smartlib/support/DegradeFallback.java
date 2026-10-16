package com.smartlib.support;

import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

import org.springframework.stereotype.Component;

import com.smartlib.domain.dto.Dtos.RecommendItem;
import com.smartlib.domain.dto.Dtos.SearchResult;
import com.smartlib.domain.dto.Dtos.StockDeductResult;
import com.smartlib.domain.dto.Dtos.StockView;

/**
 * 降级兜底返回值集中定义。
 *
 * <p>降级不是"随便返回个默认值"，而是要满足三个条件：
 * <ol>
 *   <li><b>语义正确</b>：不能把"查不到"伪装成"库存为 0"，否则读者会以为自己借不到书。
 *       因此库存未知时用 {@code availableCopies = -1} 表示"暂时无法确认"，
 *       由前端与 Agent 渲染成"库存暂时无法确认，请稍后再试"；</li>
 *   <li><b>显式可观测</b>：每个降级结果都带 degraded 标记与原因，监控可以按原因分别告警；</li>
 *   <li><b>有损但不误导</b>：推荐降级到热门榜是"相关性下降"，而不是"给错数据"。</li>
 * </ol>
 */
@Component
public class DegradeFallback {

    /** 库存未知的约定值：负数表示"无法确认"，与 availableCopies=0（明确无库存）语义不同。 */
    public static final int STOCK_UNKNOWN = -1;

    private final Map<String, AtomicLong> degradeCounters = new ConcurrentHashMap<>();

    /** 检索降级：返回空结果并标记原因，绝不返回"看起来像真实结果"的假数据。 */
    public SearchResult search(String reason) {
        count("search:" + reason);
        return new SearchResult(List.of(), 0L, 0L, "degraded", true, reason);
    }

    /** 库存降级：可用数置为"未知"，前端必须按未知渲染。 */
    public StockView stock(Long bookId, String title, int totalCopies, String shelfLocation, String reason) {
        count("stock:" + reason);
        return new StockView(bookId, title, totalCopies, STOCK_UNKNOWN, -1, shelfLocation);
    }

    /**
     * 推荐降级：退化为热门榜（相关性下降但内容真实），
     * 而不是返回空列表 —— 空列表对 Agent 的对话体验伤害更大。
     */
    public List<RecommendItem> recommend(List<RecommendItem> hotBooks, String reason) {
        count("recommend:" + reason);
        return hotBooks.stream()
                .map(item -> new RecommendItem(item.book(), item.score(), "降级为热门榜单", "degrade:" + reason))
                .toList();
    }

    /** 秒杀式抢书降级：明确告知失败并建议重试，不允许"乐观地返回成功"。 */
    public StockDeductResult stockDeduct(String reason) {
        count("stock-deduct:" + reason);
        return StockDeductResult.fail("degraded", reason, 0);
    }

    public Map<String, Long> snapshot() {
        Map<String, Long> result = new ConcurrentHashMap<>();
        degradeCounters.forEach((k, v) -> result.put(k, v.get()));
        return result;
    }

    private void count(String key) {
        degradeCounters.computeIfAbsent(key, k -> new AtomicLong()).incrementAndGet();
    }
}
