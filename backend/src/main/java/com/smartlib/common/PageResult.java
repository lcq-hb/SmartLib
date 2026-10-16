package com.smartlib.common;

import java.util.List;

/**
 * 统一分页结果。
 *
 * @param list  当前页数据
 * @param total 满足条件的总条数
 * @param page  页码，从 1 开始
 * @param size  每页条数
 */
public record PageResult<T>(List<T> list, long total, int page, int size) {

    public static <T> PageResult<T> of(List<T> list, long total, int page, int size) {
        return new PageResult<>(list, total, page, size);
    }

    public static <T> PageResult<T> empty(int page, int size) {
        return new PageResult<>(List.of(), 0L, page, size);
    }

    public int totalPages() {
        return size <= 0 ? 0 : (int) ((total + size - 1) / size);
    }
}
