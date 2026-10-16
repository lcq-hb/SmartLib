package com.smartlib.service;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.smartlib.domain.dto.BookRow;
import com.smartlib.domain.dto.Dtos.BookView;
import com.smartlib.domain.dto.Dtos.LoanView;
import com.smartlib.domain.dto.Dtos.RecommendItem;
import com.smartlib.domain.dto.LoanRow;
import com.smartlib.domain.dto.NamedCount;
import com.smartlib.domain.entity.BorrowRecord;
import com.smartlib.domain.entity.Reader;
import com.smartlib.mapper.BookMapper;
import com.smartlib.mapper.BorrowRecordMapper;
import com.smartlib.mapper.ReaderMapper;
import com.smartlib.service.search.BookViews;

/**
 * 读者服务：我的借阅、信用分、读者画像。
 */
@Service
public class ReaderService {

    private final ReaderMapper readerMapper;
    private final BorrowRecordMapper borrowMapper;
    private final BookMapper bookMapper;
    private final RuleEngine ruleEngine;

    public ReaderService(ReaderMapper readerMapper,
                         BorrowRecordMapper borrowMapper,
                         BookMapper bookMapper,
                         RuleEngine ruleEngine) {
        this.readerMapper = readerMapper;
        this.borrowMapper = borrowMapper;
        this.bookMapper = bookMapper;
        this.ruleEngine = ruleEngine;
    }

    @Transactional(readOnly = true)
    public Reader requireReader(long readerId) {
        Reader reader = readerMapper.findById(readerId);
        if (reader == null) {
            throw new com.smartlib.common.BizException(com.smartlib.common.ErrorCode.READER_NOT_FOUND,
                    "读者不存在：id=" + readerId);
        }
        return reader;
    }

    /**
     * "我的借阅"列表。
     *
     * <p>用一次 join 查询拿到书名与馆藏位置，避免"先查记录再逐条查书"的 N+1 问题
     * （20 条记录就是 21 次查询，这是最典型的性能陷阱之一）。
     */
    @Transactional(readOnly = true)
    public List<LoanView> myLoans(long readerId, int limit) {
        requireReader(readerId);
        int size = Math.min(Math.max(limit, 1), 100);
        List<LoanRow> rows = borrowMapper.listLoanRows(readerId, size);
        LocalDateTime now = LocalDateTime.now();
        List<LoanView> result = new ArrayList<>(rows.size());
        for (LoanRow row : rows) {
            int overdueDays = 0;
            if (row.getDueAt() != null && !"RETURNED".equals(row.getStatus()) && now.isAfter(row.getDueAt())) {
                overdueDays = (int) java.time.Duration.between(row.getDueAt(), now).toDays();
                if (overdueDays == 0) {
                    overdueDays = 1;
                }
            }
            boolean active = "BORROWED".equals(row.getStatus()) || "OVERDUE".equals(row.getStatus());
            result.add(new LoanView(
                    row.getBorrowId(), row.getBookId(), row.getTitle(), row.getAuthor(), row.getShelfLocation(),
                    row.getBorrowAt(), row.getDueAt(), overdueDays,
                    row.getRenewCount() == null ? 0 : row.getRenewCount(),
                    active && overdueDays == 0,
                    row.getStatus(),
                    row.getFine() == null ? BigDecimal.ZERO : row.getFine()));
        }
        return result;
    }

    /**
     * 读者画像：Agent 做个性化推荐时的上下文。
     *
     * <p>包含"偏好分类"与"最近借过的书名"，两者都是<b>脱敏且必要</b>的信息 ——
     * 推荐需要它们，但不需要读者的身份证号、手机号等。最小必要原则在这里落地。
     */
    @Transactional(readOnly = true)
    public Map<String, Object> profile(long readerId) {
        Reader reader = requireReader(readerId);
        int activeCount = borrowMapper.countActiveByReader(readerId);
        BigDecimal unpaidFine = borrowMapper.sumUnpaidFine(readerId);
        List<NamedCount> preferred = readerMapper.findPreferredCategories(readerId, 5);
        List<BorrowRecord> recent = borrowMapper.listByReader(readerId, 5);

        List<String> recentTitles = new ArrayList<>();
        for (BorrowRecord record : recent) {
            var book = bookMapper.findById(record.getBookId());
            if (book != null) {
                recentTitles.add(book.getTitle());
            }
        }

        Map<String, Object> profile = new LinkedHashMap<>();
        profile.put("readerId", reader.getId());
        profile.put("cardNo", reader.getCardNo());
        profile.put("name", reader.getName());
        profile.put("readerType", reader.getType());
        profile.put("dept", reader.getDept());
        profile.put("creditScore", reader.getCreditScore());
        profile.put("status", reader.getStatus());
        profile.put("activeBorrowCount", activeCount);
        profile.put("maxBorrowCount", ruleEngine.ruleFor(reader.getType()).maxBorrowCount());
        profile.put("loanDays", ruleEngine.ruleFor(reader.getType()).loanDays());
        profile.put("unpaidFine", unpaidFine);
        profile.put("preferredCategories", preferred.stream().map(NamedCount::getName).toList());
        profile.put("recentTitles", recentTitles);
        return profile;
    }

    @Transactional(readOnly = true)
    public Map<String, Object> credit(long readerId) {
        Reader reader = requireReader(readerId);
        return Map.of(
                "readerId", reader.getId(),
                "creditScore", reader.getCreditScore(),
                "status", reader.getStatus(),
                "unpaidFine", borrowMapper.sumUnpaidFine(readerId),
                "overdueCount", borrowMapper.countOverdueByReader(readerId),
                "minCreditScore", ruleEngine.ruleFor(reader.getType()).minCreditScore());
    }

    /** 批量取书目，供推荐与借阅列表复用，避免 N+1。 */
    @Transactional(readOnly = true)
    Map<Long, BookView> batchBooks(Set<Long> bookIds) {
        if (bookIds.isEmpty()) {
            return Map.of();
        }
        List<BookRow> rows = bookMapper.findRowsByIds(new ArrayList<>(bookIds));
        Map<Long, BookView> result = new LinkedHashMap<>(rows.size());
        for (BookRow row : rows) {
            result.put(row.getId(), BookViews.toView(row));
        }
        return result;
    }

    @Transactional(readOnly = true)
    Set<Long> borrowedBookIds(long readerId) {
        Set<Long> ids = new HashSet<>();
        for (BorrowRecord record : borrowMapper.listByReader(readerId, 50)) {
            ids.add(record.getBookId());
        }
        return ids;
    }
}
