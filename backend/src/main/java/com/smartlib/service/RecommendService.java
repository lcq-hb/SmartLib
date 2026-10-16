package com.smartlib.service;

import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.smartlib.domain.dto.BookRow;
import com.smartlib.domain.dto.Dtos.BookView;
import com.smartlib.domain.dto.Dtos.RecommendItem;
import com.smartlib.domain.entity.Book;
import com.smartlib.domain.entity.BorrowRecord;
import com.smartlib.mapper.BookMapper;
import com.smartlib.mapper.BorrowRecordMapper;
import com.smartlib.service.search.BookViews;
import com.smartlib.support.CircuitBreaker;
import com.smartlib.support.DegradeFallback;

/**
 * 推荐服务：多路召回 + 加权排序 + 可解释理由。
 *
 * <p><b>为什么不做"一个模型算分"</b>：图书馆的推荐有三个现实约束 ——
 * 冷启动读者（新生的借阅历史是空的）、书目只有几万到上百万、
 * 而且必须能对读者解释"为什么推荐这本书"（读者会问，馆员也会问）。
 * 多路召回 + 加权融合正好满足：每一路都是一个可解释的规则，
 * 冷启动时协同过滤那一路自然为空，内容与热门两路顶上。
 *
 * <p><b>四路召回</b>（权重体现可靠性差异）：
 * <table>
 *   <tr><th>通道</th><th>权重</th><th>依据</th></tr>
 *   <tr><td>协同过滤（借了 A 的人还借了 B）</td><td>1.5</td><td>行为共现，命中率最高</td></tr>
 *   <tr><td>内容相似（同类目热门）</td><td>1.0</td><td>稳定，冷启动可用</td></tr>
 *   <tr><td>读者画像（近期偏好类目）</td><td>0.8</td><td>个性化</td></tr>
 *   <tr><td>全站热门</td><td>0.5</td><td>兜底与多样性</td></tr>
 * </table>
 *
 * <p><b>可借数加成</b>：再加 {@code 0.3 × min(available, 3) / 3}。
 * 推荐一本借不到的书对读者是负体验，这个加成让"能立刻借到"的书略微靠前。
 * 它刻意做得很小，避免把推荐变成"库存排序"。
 */
@Service
public class RecommendService {

    private static final Logger log = LoggerFactory.getLogger(RecommendService.class);

    private static final double W_CO_BORROW = 1.5;
    private static final double W_CONTENT = 1.0;
    private static final double W_PROFILE = 0.8;
    private static final double W_HOT = 0.5;
    private static final double W_AVAILABILITY = 0.3;

    /** 热门统计窗口。 */
    private static final int HOT_WINDOW_DAYS = 90;
    /** 用多少条近期借阅作为召回种子。太多会让推荐发散。 */
    private static final int SEED_SIZE = 3;

    private final BookMapper bookMapper;
    private final BorrowRecordMapper borrowMapper;
    private final CircuitBreaker circuitBreaker;
    private final DegradeFallback degradeFallback;

    public RecommendService(BookMapper bookMapper,
                            BorrowRecordMapper borrowMapper,
                            CircuitBreaker circuitBreaker,
                            DegradeFallback degradeFallback) {
        this.bookMapper = bookMapper;
        this.borrowMapper = borrowMapper;
        this.circuitBreaker = circuitBreaker;
        this.degradeFallback = degradeFallback;
    }

    /**
     * 为读者生成推荐。
     *
     * @param readerId 读者 id；为 null 时退化为"全站热门"（新用户冷启动）
     */
    @Transactional(readOnly = true)
    public List<RecommendItem> recommendForReader(Long readerId, int limit) {
        int size = Math.min(Math.max(limit, 1), 20);
        List<RecommendItem> hotBooks = hotRecall(size * 2);

        if (readerId == null) {
            return hotBooks.stream().limit(size).toList();
        }

        return circuitBreaker.call("recommend-reader",
                () -> doRecommend(readerId, size, hotBooks),
                () -> degradeFallback.recommend(hotBooks, "推荐服务异常"));
    }

    private List<RecommendItem> doRecommend(long readerId, int size, List<RecommendItem> hotBooks) {
        Map<Long, Accumulator> merged = new LinkedHashMap<>();

        // 排除读者已在借的书：推荐一本正在手里的书毫无意义
        Set<Long> owned = new HashSet<>();
        for (BorrowRecord record : borrowMapper.listByReader(readerId, 50)) {
            if ("BORROWED".equals(record.getStatus()) || "OVERDUE".equals(record.getStatus())) {
                owned.add(record.getBookId());
            }
        }

        // ---- 召回种子：最近借过的书 ----
        List<BorrowRecord> recent = borrowMapper.listByReader(readerId, SEED_SIZE);
        for (BorrowRecord seed : recent) {
            Book seedBook = bookMapper.findById(seed.getBookId());
            if (seedBook == null) {
                continue;
            }
            // 通道 1：协同过滤
            accumulate(merged, bookMapper.findCoBorrowed(seed.getBookId(), 10), W_CO_BORROW, "co-borrow",
                    "借过《" + seedBook.getTitle() + "》的读者也借过");
            // 通道 2：内容相似
            accumulate(merged, bookMapper.findSimilar(seedBook.getCategoryId(), List.of(seed.getBookId()),
                            LocalDateTime.now().minusDays(HOT_WINDOW_DAYS), 10), W_CONTENT, "content",
                    "与《" + seedBook.getTitle() + "》同类");
            // 通道 3：读者画像（用种子书的类目近似"近期偏好"）
            accumulate(merged, bookMapper.findSimilar(seedBook.getCategoryId(), List.of(),
                            LocalDateTime.now().minusDays(HOT_WINDOW_DAYS), 5), W_PROFILE, "profile",
                    "符合你近期的阅读偏好");
        }

        // 通道 4：全站热门兜底
        for (RecommendItem item : hotBooks) {
            merged.computeIfAbsent(item.book().id(), id -> new Accumulator(item.book(), W_HOT, "hot", "当前热门"));
        }

        // ---- 排序 ----
        List<RecommendItem> ranked = new ArrayList<>();
        for (Accumulator accumulator : merged.values()) {
            if (owned.contains(accumulator.book.id())) {
                continue;
            }
            double availabilityBoost = W_AVAILABILITY * Math.min(accumulator.book.availableCopies(), 3) / 3.0;
            ranked.add(new RecommendItem(accumulator.book, accumulator.score + availabilityBoost,
                    accumulator.reason, accumulator.source));
        }
        ranked.sort(Comparator.comparingDouble(RecommendItem::score).reversed());
        log.debug("推荐生成 readerId={} 候选={} 输出={}", readerId, merged.size(), Math.min(size, ranked.size()));
        return ranked.stream().limit(size).toList();
    }

    /**
     * 与某本书相似的推荐（"同书推荐"，Agent 的类似书问答用它）。
     */
    @Transactional(readOnly = true)
    public List<RecommendItem> similarTo(long bookId, int limit) {
        int size = Math.min(Math.max(limit, 1), 20);
        Book book = bookMapper.findById(bookId);
        if (book == null) {
            return List.of();
        }
        Map<Long, Accumulator> merged = new LinkedHashMap<>();
        accumulate(merged, bookMapper.findCoBorrowed(bookId, size), W_CO_BORROW, "co-borrow",
                "借过《" + book.getTitle() + "》的读者也借过");
        accumulate(merged, bookMapper.findSimilar(book.getCategoryId(), List.of(bookId),
                LocalDateTime.now().minusDays(HOT_WINDOW_DAYS), size), W_CONTENT, "content",
                "与《" + book.getTitle() + "》同类");

        List<RecommendItem> ranked = new ArrayList<>();
        merged.forEach((id, acc) -> ranked.add(new RecommendItem(acc.book, acc.score, acc.reason, acc.source)));
        ranked.sort(Comparator.comparingDouble(RecommendItem::score).reversed());
        return ranked.stream().limit(size).toList();
    }

    private List<RecommendItem> hotRecall(int limit) {
        List<BookRow> rows = bookMapper.findHot(LocalDateTime.now().minusDays(HOT_WINDOW_DAYS), limit);
        List<RecommendItem> items = new ArrayList<>(rows.size());
        int rank = 0;
        for (BookRow row : rows) {
            BookView view = BookViews.toView(row);
            // 名次越靠前权重越高，避免把"第 20 名热门"与"第 1 名热门"等同看待
            double score = W_HOT * (1.0 - Math.min(rank, 10) * 0.05);
            items.add(new RecommendItem(view, score, "当前热门", "hot"));
            rank++;
        }
        return items;
    }

    /**
     * 累加某一路召回的得分。同一本书被多路命中时取"加权和"而不是最大值 ——
     * 多路同时命中本身就是强信号，应该加分。
     */
    private void accumulate(Map<Long, Accumulator> merged, List<BookRow> rows, double weight,
                            String source, String reason) {
        int rank = 0;
        for (BookRow row : rows) {
            BookView view = BookViews.toView(row);
            double score = weight * (1.0 - Math.min(rank, 10) * 0.05);
            Accumulator existing = merged.get(view.id());
            if (existing == null) {
                merged.put(view.id(), new Accumulator(view, score, source, reason));
            } else {
                existing.score += score;
                // 保留更强通道的解释，读者更容易理解"因为别人也借了"而不是"因为同类"
                if (rankOf(score) >= rankOf(existing.bestScore)) {
                    existing.reason = reason;
                    existing.source = source;
                    existing.bestScore = score;
                }
            }
            rank++;
        }
    }

    private double rankOf(double score) {
        return score;
    }

    /** 累加器：可变对象，避免在循环里反复创建 record。 */
    private static final class Accumulator {
        private final BookView book;
        private double score;
        private double bestScore;
        private String source;
        private String reason;

        Accumulator(BookView book, double score, String source, String reason) {
            this.book = book;
            this.score = score;
            this.bestScore = score;
            this.source = source;
            this.reason = reason;
        }
    }
}
