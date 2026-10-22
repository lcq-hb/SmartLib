package com.smartlib.service.search;

import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.Dtos.SearchResult;

/**
 * 检索引擎 SPI。
 *
 * <p>蓝图里的 Q1~Q5 是<b>方案对比</b>，落到代码上目前有 3 个实现：
 * {@code like}（基线）、{@code inverted-index}（自研倒排）、{@code elasticsearch}。
 * 其中前两个用于实测对比；前缀索引（Q2）与 MySQL ngram（Q4）只做了方案比较，没有实现。
 * 三个实现都收在这一个接口后面，因此：
 * <ul>
 *   <li>切换引擎只改配置 {@code smartlib.search.engine}，业务代码零改动；</li>
 *   <li>压测脚本可以用同一批请求跑完所有<b>已实现</b>的引擎，得到可直接对比的数据；</li>
 *   <li>其中一个引擎故障时可以降级到另一个（如倒排索引异常 → 降级 DB 直查）。</li>
 * </ul>
 *
 * <p>实现者必须保证：<b>返回结果中的 availableCopies 是"可能略旧"的值</b>，
 * 因为它可能来自索引或缓存。强一致的实时库存只能通过库存接口获取，
 * 这一点在 Agent 侧的工具设计上被强制执行（库存类问答必须调 get_stock 工具）。
 */
public interface SearchEngine {

    /** 引擎名，与配置项 {@code smartlib.search.engine} 取值一致。 */
    String name();

    SearchResult search(BookQuery query);

    /** 是否支持"关键词全文检索"。false 表示只支持条件筛选（如纯数据库方案在无关键词时反而更优）。 */
    default boolean supportsFullText() {
        return true;
    }

    /** 已索引文档数；不适用时返回 -1。 */
    default int indexedDocCount() {
        return -1;
    }

    /** 该引擎是否可用于线上流量（例如 ES 实例未就绪时应返回 false）。 */
    default boolean ready() {
        return true;
    }
}
