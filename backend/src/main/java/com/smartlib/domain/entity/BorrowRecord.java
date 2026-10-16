package com.smartlib.domain.entity;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 借阅流水实体。
 *
 * <p>本类由 scripts/tools/gen_entities.py 生成，请勿手工修改字段：
 * 需要调整字段请修改生成器中的字段清单后重新生成（与
 * backend/src/main/resources/db/schema.sql 一一对应）。
 */
public class BorrowRecord {

    private Long id;
    private Long readerId;
    private Long bookId;
    private LocalDateTime borrowAt;
    private LocalDateTime dueAt;
    private LocalDateTime returnAt;
    private Integer renewCount;
    private String status;
    private BigDecimal fine;
    private LocalDateTime createdAt;
    private LocalDateTime updatedAt;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public Long getReaderId() {
        return readerId;
    }

    public void setReaderId(Long readerId) {
        this.readerId = readerId;
    }

    public Long getBookId() {
        return bookId;
    }

    public void setBookId(Long bookId) {
        this.bookId = bookId;
    }

    public LocalDateTime getBorrowAt() {
        return borrowAt;
    }

    public void setBorrowAt(LocalDateTime borrowAt) {
        this.borrowAt = borrowAt;
    }

    public LocalDateTime getDueAt() {
        return dueAt;
    }

    public void setDueAt(LocalDateTime dueAt) {
        this.dueAt = dueAt;
    }

    public LocalDateTime getReturnAt() {
        return returnAt;
    }

    public void setReturnAt(LocalDateTime returnAt) {
        this.returnAt = returnAt;
    }

    public Integer getRenewCount() {
        return renewCount;
    }

    public void setRenewCount(Integer renewCount) {
        this.renewCount = renewCount;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public BigDecimal getFine() {
        return fine;
    }

    public void setFine(BigDecimal fine) {
        this.fine = fine;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(LocalDateTime createdAt) {
        this.createdAt = createdAt;
    }

    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }

    public void setUpdatedAt(LocalDateTime updatedAt) {
        this.updatedAt = updatedAt;
    }

    @Override
    public String toString() {
        return "BorrowRecord{"
                + "id=" + id + ", "
                + "readerId=" + readerId + ", "
                + "bookId=" + bookId + ", "
                + "borrowAt=" + borrowAt + ", "
                + "dueAt=" + dueAt + ", "
                + "returnAt=" + returnAt + ", "
                + "renewCount=" + renewCount + ", "
                + "status=" + status + ", "
                + "fine=" + fine + ", "
                + "createdAt=" + createdAt + ", "
                + "updatedAt=" + updatedAt + ", "
                + '}';
    }
}
