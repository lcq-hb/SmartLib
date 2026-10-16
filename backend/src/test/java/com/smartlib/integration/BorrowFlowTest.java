package com.smartlib.integration;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import com.smartlib.common.BizException;
import com.smartlib.domain.dto.Dtos.BorrowResult;
import com.smartlib.domain.entity.Book;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.entity.ReaderFixture;
import com.smartlib.mapper.BookMapper;
import com.smartlib.mapper.BookStockMapper;
import com.smartlib.mapper.BorrowRecordMapper;
import com.smartlib.mapper.StockLedgerMapper;
import com.smartlib.service.BorrowService;
import com.smartlib.service.StockService;
import com.smartlib.service.stock.StockStrategyFactory;

/**
 * 借阅业务链路集成测试：幂等、规则、归还、对账。
 */
@SpringBootTest
@ActiveProfiles("test")
class BorrowFlowTest {

    @Autowired
    private BorrowService borrowService;
    @Autowired
    private StockService stockService;
    @Autowired
    private BookMapper bookMapper;
    @Autowired
    private BookStockMapper stockMapper;
    @Autowired
    private BorrowRecordMapper borrowMapper;
    @Autowired
    private StockLedgerMapper ledgerMapper;
    @Autowired
    private StockStrategyFactory strategyFactory;
    @Autowired
    private ReaderFixture readerFixture;

    @Test
    @DisplayName("幂等：同一个 Idempotency-Key 重复提交只会借出一本")
    void idempotentBorrow() {
        strategyFactory.switchTo("optimistic-db");
        long bookId = newBook(3);
        long readerId = readerFixture.newReader("UNDERGRAD", 100);
        String idemKey = "itest-idem-" + System.nanoTime();

        BorrowResult first = borrowService.borrow(readerId, bookId, idemKey);
        assertThat(first.ok()).isTrue();
        assertThat(first.idempotentReplay()).isFalse();

        // 第二次用同一个 key：必须返回首次结果，且是"重放"标记
        BorrowResult replay = borrowService.borrow(readerId, bookId, idemKey);
        assertThat(replay.ok()).isTrue();
        assertThat(replay.idempotentReplay()).isTrue();
        assertThat(replay.borrowId()).isEqualTo(first.borrowId());

        // 库存只被扣一次
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isEqualTo(2);
        // 借阅记录也只有一条
        assertThat(borrowMapper.countActiveByReader(readerId)).isEqualTo(1);
    }

    @Test
    @DisplayName("幂等：不同 key 但同一本书，第二次必须被规则拦下（同一读者不能重复借同一本书）")
    void sameBookCannotBorrowTwice() {
        strategyFactory.switchTo("optimistic-db");
        long bookId = newBook(3);
        long readerId = readerFixture.newReader("UNDERGRAD", 100);

        assertThat(borrowService.borrow(readerId, bookId, "dup-a-" + System.nanoTime()).ok()).isTrue();

        assertThatThrownBy(() -> borrowService.borrow(readerId, bookId, "dup-b-" + System.nanoTime()))
                .isInstanceOf(BizException.class)
                .hasMessageContaining("重复借阅");
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isEqualTo(2);
    }

    @Test
    @DisplayName("规则：信用分低于阈值禁止借阅（阈值来自 rule_config，改配置即生效）")
    void lowCreditIsRejected() {
        strategyFactory.switchTo("optimistic-db");
        long bookId = newBook(2);
        long readerId = readerFixture.newReader("UNDERGRAD", 55);

        assertThatThrownBy(() -> borrowService.borrow(readerId, bookId, "credit-" + System.nanoTime()))
                .isInstanceOf(BizException.class)
                .hasMessageContaining("信用分");
        // 被规则拦下时不允许扣库存
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isEqualTo(2);
    }

    @Test
    @DisplayName("超卖：库存耗尽后借书返回失败结果，且库存保持为 0 不为负")
    void stockExhaustedReturnsFailure() {
        strategyFactory.switchTo("optimistic-db");
        long bookId = newBook(1);
        long readerA = readerFixture.newReader("UNDERGRAD", 100);
        long readerB = readerFixture.newReader("UNDERGRAD", 100);

        assertThat(borrowService.borrow(readerA, bookId, "exhaust-a-" + System.nanoTime()).ok()).isTrue();

        BorrowResult second = borrowService.borrow(readerB, bookId, "exhaust-b-" + System.nanoTime());
        assertThat(second.ok()).isFalse();
        assertThat(second.reason()).contains("库存不足");
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isZero();
    }

    @Test
    @DisplayName("归还：库存回补、流水配对，且不会把库存加爆")
    void returnBookRestoresStock() {
        strategyFactory.switchTo("optimistic-db");
        long bookId = newBook(2);
        long readerId = readerFixture.newReader("POSTGRAD", 100);

        BorrowResult borrow = borrowService.borrow(readerId, bookId, "ret-" + System.nanoTime());
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isEqualTo(1);

        BorrowResult returned = borrowService.returnBook(borrow.borrowId());
        assertThat(returned.ok()).isTrue();
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isEqualTo(2);

        // 再还一次必须失败：靠 status 条件更新兜底，不允许把库存重复加回去
        assertThatThrownBy(() -> borrowService.returnBook(borrow.borrowId()))
                .isInstanceOf(BizException.class);
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isEqualTo(2);

        // 归还后流水与库存必须仍然守恒
        assertThat(ledgerMapper.sumDelta(bookId)).isZero();
        assertThat(stockService.reconcile()).isEmpty();
    }

    @Test
    @DisplayName("归还：并发重复归还只会有一次成功（状态条件更新防重复加库存）")
    void concurrentReturnOnlyOnce() throws Exception {
        strategyFactory.switchTo("optimistic-db");
        long bookId = newBook(1);
        long readerId = readerFixture.newReader("UNDERGRAD", 100);
        BorrowResult borrow = borrowService.borrow(readerId, bookId, "cret-" + System.nanoTime());
        assertThat(borrow.ok()).isTrue();

        int threads = 20;
        AtomicInteger success = new AtomicInteger();
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        CountDownLatch gate = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(threads);
        for (int i = 0; i < threads; i++) {
            pool.submit(() -> {
                try {
                    gate.await();
                    borrowService.returnBook(borrow.borrowId());
                    success.incrementAndGet();
                } catch (Exception ignored) {
                    // 预期中的失败：说明并发重复归还被正确拦住
                } finally {
                    done.countDown();
                }
            });
        }
        gate.countDown();
        assertThat(done.await(30, TimeUnit.SECONDS)).isTrue();
        pool.shutdownNow();

        assertThat(success.get()).as("20 个并发归还请求只允许一个成功").isEqualTo(1);
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isEqualTo(1);
        assertThat(stockService.reconcile()).isEmpty();
    }

    @Test
    @DisplayName("对账：多轮借还之后库存与流水仍然守恒（不变式可回归）")
    void reconcileStaysConsistent() {
        strategyFactory.switchTo("optimistic-db");
        long bookId = newBook(5);
        List<Long> readers = new ArrayList<>();
        for (int i = 0; i < 5; i++) {
            readers.add(readerFixture.newReader("TEACHER", 100));
        }
        List<Long> borrowIds = new ArrayList<>();
        for (Long readerId : readers) {
            BorrowResult result = borrowService.borrow(readerId, bookId, "recon-" + readerId + "-" + System.nanoTime());
            assertThat(result.ok()).isTrue();
            borrowIds.add(result.borrowId());
        }
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isZero();
        assertThat(ledgerMapper.sumDelta(bookId)).isEqualTo(-5);

        // 归还其中 3 本
        for (int i = 0; i < 3; i++) {
            borrowService.returnBook(borrowIds.get(i));
        }
        assertThat(stockMapper.findByBookId(bookId).getAvailableCopies()).isEqualTo(3);
        assertThat(ledgerMapper.sumDelta(bookId)).isEqualTo(-2);
        assertThat(stockService.reconcile()).as("多轮借还后不允许出现任何对账不一致").isEmpty();
    }

    private long newBook(int copies) {
        Book book = new Book();
        book.setIsbn("FLOW-" + System.nanoTime());
        book.setTitle("流程测试用书");
        book.setAuthor("测试");
        book.setPrice(new BigDecimal("30.00"));
        book.setTotalCopies(copies);
        bookMapper.insert(book);

        BookStock stock = new BookStock();
        stock.setBookId(book.getId());
        stock.setTotalCopies(copies);
        stock.setAvailableCopies(copies);
        stock.setLockedCopies(0);
        stock.setVersion(0);
        stock.setUpdatedAt(java.time.LocalDateTime.now());
        stockMapper.insert(stock);
        return book.getId();
    }
}
