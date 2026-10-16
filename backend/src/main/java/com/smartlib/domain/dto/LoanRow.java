package com.smartlib.domain.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 借阅列表读模型（borrow_record + book 的投影），用于"我的借阅"。
 *
 * <p>本类由 scripts/tools/gen_entities.py 生成，请勿手工修改字段：
 * 需要调整字段请修改生成器中的字段清单后重新生成（与
 * backend/src/main/resources/db/schema.sql 一一对应）。
 */
public class LoanRow {

    private Long borrowId;
    private Long bookId;
    private String title;
    private String author;
    private String shelfLocation;
    private LocalDateTime borrowAt;
    private LocalDateTime dueAt;
    private Integer renewCount;
    private String status;
    private BigDecimal fine;

    public Long getBorrowId() {
        return borrowId;
    }

    public void setBorrowId(Long borrowId) {
        this.borrowId = borrowId;
    }

    public Long getBookId() {
        return bookId;
    }

    public void setBookId(Long bookId) {
        this.bookId = bookId;
    }

    public String getTitle() {
        return title;
    }

    public void setTitle(String title) {
        this.title = title;
    }

    public String getAuthor() {
        return author;
    }

    public void setAuthor(String author) {
        this.author = author;
    }

    public String getShelfLocation() {
        return shelfLocation;
    }

    public void setShelfLocation(String shelfLocation) {
        this.shelfLocation = shelfLocation;
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

    @Override
    public String toString() {
        return "LoanRow{"
                + "borrowId=" + borrowId + ", "
                + "bookId=" + bookId + ", "
                + "title=" + title + ", "
                + "author=" + author + ", "
                + "shelfLocation=" + shelfLocation + ", "
                + "borrowAt=" + borrowAt + ", "
                + "dueAt=" + dueAt + ", "
                + "renewCount=" + renewCount + ", "
                + "status=" + status + ", "
                + "fine=" + fine + ", "
                + '}';
    }
}
