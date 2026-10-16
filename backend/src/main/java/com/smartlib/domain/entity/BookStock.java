package com.smartlib.domain.entity;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 库存实体，全系统库存的唯一事实源，availableCopies 永不允许为负。
 *
 * <p>本类由 scripts/tools/gen_entities.py 生成，请勿手工修改字段：
 * 需要调整字段请修改生成器中的字段清单后重新生成（与
 * backend/src/main/resources/db/schema.sql 一一对应）。
 */
public class BookStock {

    private Long bookId;
    private Integer totalCopies;
    private Integer availableCopies;
    private Integer lockedCopies;
    private Integer version;
    private LocalDateTime updatedAt;

    public Long getBookId() {
        return bookId;
    }

    public void setBookId(Long bookId) {
        this.bookId = bookId;
    }

    public Integer getTotalCopies() {
        return totalCopies;
    }

    public void setTotalCopies(Integer totalCopies) {
        this.totalCopies = totalCopies;
    }

    public Integer getAvailableCopies() {
        return availableCopies;
    }

    public void setAvailableCopies(Integer availableCopies) {
        this.availableCopies = availableCopies;
    }

    public Integer getLockedCopies() {
        return lockedCopies;
    }

    public void setLockedCopies(Integer lockedCopies) {
        this.lockedCopies = lockedCopies;
    }

    public Integer getVersion() {
        return version;
    }

    public void setVersion(Integer version) {
        this.version = version;
    }

    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }

    public void setUpdatedAt(LocalDateTime updatedAt) {
        this.updatedAt = updatedAt;
    }

    @Override
    public String toString() {
        return "BookStock{"
                + "bookId=" + bookId + ", "
                + "totalCopies=" + totalCopies + ", "
                + "availableCopies=" + availableCopies + ", "
                + "lockedCopies=" + lockedCopies + ", "
                + "version=" + version + ", "
                + "updatedAt=" + updatedAt + ", "
                + '}';
    }
}
