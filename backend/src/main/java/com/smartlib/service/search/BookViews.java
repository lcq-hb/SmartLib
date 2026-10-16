package com.smartlib.service.search;

import java.time.LocalDateTime;

import com.smartlib.domain.dto.BookRow;
import com.smartlib.domain.dto.Dtos.BookView;
import com.smartlib.domain.dto.Dtos.LoanView;
import com.smartlib.domain.entity.BorrowRecord;

/**
 * 读模型与展示模型之间的转换。
 *
 * <p>集中在一个类里做转换，而不是散落在各 Service 里：
 * 一旦 BookView 增删字段，编译器会在这里报错，而不是等到运行期发现某个接口少了字段。
 */
public final class BookViews {

    private BookViews() {
    }

    public static BookView toView(BookRow row) {
        if (row == null) {
            return null;
        }
        return new BookView(
                row.getId(),
                row.getIsbn(),
                row.getTitle(),
                row.getAuthor(),
                row.getPublisher(),
                row.getCategoryName(),
                row.getPublishYear(),
                row.getPrice(),
                row.getTags(),
                row.getSummary(),
                row.getShelfLocation(),
                row.getTotalCopies() == null ? 0 : row.getTotalCopies(),
                row.getAvailableCopies() == null ? 0 : row.getAvailableCopies());
    }

    /**
     * 借阅记录 + 书目的展示模型。逾期天数在内存中计算，
     * 避免把"今天"这个变量写进 SQL（那样 SQL 不可缓存、不可测）。
     */
    public static LoanView toLoanView(BookRow book, BorrowRecord record, LocalDateTime now) {
        int overdueDays = 0;
        if (record.getDueAt() != null && record.getReturnAt() == null && now.isAfter(record.getDueAt())) {
            overdueDays = (int) java.time.Duration.between(record.getDueAt(), now).toDays();
        }
        boolean active = "BORROWED".equals(record.getStatus()) || "OVERDUE".equals(record.getStatus());
        return new LoanView(
                record.getId(),
                record.getBookId(),
                book == null ? null : book.getTitle(),
                book == null ? null : book.getAuthor(),
                book == null ? null : book.getShelfLocation(),
                record.getBorrowAt(),
                record.getDueAt(),
                overdueDays,
                record.getRenewCount() == null ? 0 : record.getRenewCount(),
                active && overdueDays == 0,
                record.getStatus(),
                record.getFine());
    }
}
