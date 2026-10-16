package com.smartlib.service.search;

import java.util.List;

import org.springframework.stereotype.Component;

import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.BookRow;
import com.smartlib.domain.dto.Dtos.SearchResult;
import com.smartlib.mapper.BookMapper;

/**
 * Q1：数据库 {@code like '%kw%'} 基线引擎。
 *
 * <p>它在生产上不是一个"方案"，而是一个<b>参照物</b>：保留它的唯一目的是让压测能给出
 * "优化前 vs 优化后"的对比数据。如果没有这个基线，说"倒排索引很快"就只是主观判断。
 *
 * <p>为什么它慢：{@code like '%kw%'} 的左模糊无法使用 B+ 树索引，
 * 只能全表扫描并逐行做字符串匹配。在 120 万行的书目表上，
 * 即使每行只花 1 微秒，也要 1.2 秒以上，再加上回表与网络传输，
 * 这就是线上 2.8s 的来源。
 *
 * <p>它也有自己的价值：<b>条件筛选（分类、年份、仅看可借）走的是索引</b>，
 * 因此"不带关键词的浏览型请求"用数据库反而比倒排索引更合适 ——
 * {@code BookSearchService} 就是按这个规则分流：有关键词走倒排，无关键词走数据库。
 */
@Component
public class LikeSearchEngine implements SearchEngine {

    public static final String NAME = "like";

    private final BookMapper bookMapper;

    public LikeSearchEngine(BookMapper bookMapper) {
        this.bookMapper = bookMapper;
    }

    @Override
    public String name() {
        return NAME;
    }

    @Override
    public SearchResult search(BookQuery query) {
        long start = System.nanoTime();
        List<BookRow> rows = bookMapper.searchByLike(query);
        long total = bookMapper.countByLike(query);
        long tookMs = (System.nanoTime() - start) / 1_000_000;
        return new SearchResult(rows.stream().map(BookViews::toView).toList(),
                total, tookMs, NAME, false, null);
    }
}
