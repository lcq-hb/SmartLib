package com.smartlib.mapper;

import java.util.List;

import org.apache.ibatis.annotations.Param;

import com.smartlib.domain.dto.BookQuery;
import com.smartlib.domain.dto.BookRow;
import com.smartlib.domain.entity.Book;

/**
 * 书目 Mapper。
 *
 * <p>本接口使用 XML 映射（{@code resources/mapper/BookMapper.xml}），因为检索涉及大量动态条件；
 * 其余 Mapper 逻辑简单，使用注解。两种方式混用在 MyBatis 中是常见做法，
 * 约定见 docs/05-编码规范与工程实践.md 第 8 章。
 *
 * <p>动态 SQL 中的排序字段不允许使用 {@code ${}} 拼接，全部用 {@code <choose>} 枚举固定子句，
 * 从根上消除 ORDER BY 注入面。
 */
public interface BookMapper {

    int insert(Book book);

    Book findById(@Param("id") Long id);

    List<BookRow> findRowsByIds(@Param("ids") List<Long> ids);

    /**
     * Q1 基线检索：{@code like '%kw%'} 全表扫描。
     * 保留它的目的不是"能用"，而是作为性能对比的基线（见解决方案手册 P2）。
     */
    List<BookRow> searchByLike(@Param("q") BookQuery query);

    long countByLike(@Param("q") BookQuery query);

    /**
     * 分页拉取用于构建倒排索引的字段，避免一次性把上百万行读进内存。
     * 必须带 order by 保证翻页稳定，否则并行构建会漏文档或重复。
     */
    List<BookRow> selectPageForIndex(@Param("offset") int offset, @Param("limit") int limit);

    /** 热门榜：按最近一段时间借阅次数排序。 */
    List<BookRow> findHot(@Param("since") java.time.LocalDateTime since, @Param("limit") int limit);

    /** 相似书推荐：同分类下的其他书目，按借阅热度排序。 */
    List<BookRow> findSimilar(@Param("categoryId") Long categoryId,
                              @Param("excludeIds") List<Long> excludeIds,
                              @Param("since") java.time.LocalDateTime since,
                              @Param("limit") int limit);

    /** 按 id 批量取详情，供倒排索引命中后回表。 */
    List<BookRow> findRowsByIdsOrdered(@Param("ids") List<Long> ids);

    /**
     * 协同过滤召回："借过这本书的人还借了什么"。
     *
     * <p>用 self-join 实现 item-based 协同过滤：找出与目标书被同一批读者借过的其他书，
     * 按共现读者数降序。这是工业界最常用的召回通道之一，
     * 因为它能发现"分类不同但人群相同"的书，是纯内容召回无法覆盖的。
     */
    List<BookRow> findCoBorrowed(@Param("bookId") Long bookId, @Param("limit") int limit);
}
