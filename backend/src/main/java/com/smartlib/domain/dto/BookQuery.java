package com.smartlib.domain.dto;

import java.util.Set;

import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;

/**
 * 书目检索条件。
 *
 * <p>用可变 POJO 而非 record：需要从 HTTP Query 参数直接绑定（@ModelAttribute），
 * 并且 {@link #normalize()} 要在绑定后就地修正非法值。
 */
public class BookQuery {

    /** 允许的排序字段白名单。禁止把前端传入的字符串直接拼进 ORDER BY，防 SQL 注入。 */
    private static final Set<String> SORT_WHITELIST =
            Set.of("relevance", "year_desc", "year_asc", "price_asc", "price_desc", "hot");

    private static final int MAX_PAGE_SIZE = 50;

    private String query;
    private String category;
    private String publisher;
    private Integer yearFrom;
    private Integer yearTo;
    private Boolean availableOnly = Boolean.FALSE;
    private String sort = "relevance";
    private int page = 1;
    private int size = 20;

    public BookQuery() {
    }

    public BookQuery(String query, int page, int size) {
        this.query = query;
        this.page = page;
        this.size = size;
    }

    /**
     * 参数归一化与合法性校验。
     *
     * @return 归一化后的自身，便于链式调用
     */
    public BookQuery normalize() {
        if (page < 1) {
            page = 1;
        }
        if (size < 1) {
            size = 20;
        }
        if (size > MAX_PAGE_SIZE) {
            size = MAX_PAGE_SIZE;
        }
        if (sort == null || !SORT_WHITELIST.contains(sort)) {
            sort = "relevance";
        }
        if (yearFrom != null && yearTo != null && yearFrom > yearTo) {
            throw new BizException(ErrorCode.PARAM_INVALID, "yearFrom 不能大于 yearTo");
        }
        if (query != null) {
            query = query.trim();
            if (query.length() > 100) {
                throw new BizException(ErrorCode.PARAM_INVALID, "检索关键词长度不能超过 100");
            }
            if (query.isEmpty()) {
                query = null;
            }
        }
        if (category != null && category.isBlank()) {
            category = null;
        }
        if (publisher != null && publisher.isBlank()) {
            publisher = null;
        }
        if (availableOnly == null) {
            availableOnly = Boolean.FALSE;
        }
        return this;
    }

    /**
     * SQL 分页偏移量。
     *
     * <p><b>必须是 {@code getOffset()} 这个标准的 getter 形式</b>：
     * MyBatis 的 {@code #{q.offset}} 是通过 OGNL 反射读取属性，只会找
     * {@code getOffset()} 方法，而不是任意同名方法。写成 {@code offset()} 时
     * 会抛 "There is no getter for property named 'offset'"。
     * 这个坑在测试里被真实触发过，因此保留方法名与注释。
     */
    public int getOffset() {
        return (page - 1) * size;
    }

    /** 兼容早期调用点的别名，内部仍走标准 getter。 */
    public int offset() {
        return getOffset();
    }

    public boolean hasQuery() {
        return query != null && !query.isBlank();
    }

    public String getQuery() {
        return query;
    }

    public void setQuery(String query) {
        this.query = query;
    }

    public String getCategory() {
        return category;
    }

    public void setCategory(String category) {
        this.category = category;
    }

    public String getPublisher() {
        return publisher;
    }

    public void setPublisher(String publisher) {
        this.publisher = publisher;
    }

    public Integer getYearFrom() {
        return yearFrom;
    }

    public void setYearFrom(Integer yearFrom) {
        this.yearFrom = yearFrom;
    }

    public Integer getYearTo() {
        return yearTo;
    }

    public void setYearTo(Integer yearTo) {
        this.yearTo = yearTo;
    }

    public Boolean getAvailableOnly() {
        return availableOnly;
    }

    public void setAvailableOnly(Boolean availableOnly) {
        this.availableOnly = availableOnly;
    }

    public String getSort() {
        return sort;
    }

    public void setSort(String sort) {
        this.sort = sort;
    }

    public int getPage() {
        return page;
    }

    public void setPage(int page) {
        this.page = page;
    }

    public int getSize() {
        return size;
    }

    public void setSize(int size) {
        this.size = size;
    }
}
