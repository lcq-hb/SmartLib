package com.smartlib.mapper;

import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import com.smartlib.domain.dto.LoanRow;
import com.smartlib.domain.entity.BorrowRecord;

/**
 * 借阅流水 Mapper。
 *
 * <p>注意"在借"的判定口径：{@code status in ('BORROWED','OVERDUE')}。
 * 逾期并不是一个独立状态，它仍然占用读者的可借册数，因此所有册数统计都必须包含 OVERDUE，
 * 这是需求评审里被专门确认过的口径（见 docs/reviews/01-需求评审纪要.md）。
 */
public interface BorrowRecordMapper {

    @Insert("""
            insert into borrow_record(reader_id, book_id, borrow_at, due_at, return_at,
                                      renew_count, status, fine, created_at, updated_at)
            values(#{readerId}, #{bookId}, #{borrowAt}, #{dueAt}, #{returnAt},
                   #{renewCount}, #{status}, #{fine}, now(), now())
            """)
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(BorrowRecord record);

    @Select("select * from borrow_record where id = #{id}")
    BorrowRecord findById(@Param("id") Long id);

    @Select("""
            select count(1) from borrow_record
             where reader_id = #{readerId} and status in ('BORROWED', 'OVERDUE')
            """)
    int countActiveByReader(@Param("readerId") Long readerId);

    @Select("""
            select count(1) from borrow_record
             where reader_id = #{readerId} and book_id = #{bookId} and status in ('BORROWED', 'OVERDUE')
            """)
    int countActiveByReaderAndBook(@Param("readerId") Long readerId, @Param("bookId") Long bookId);

    @Select("select count(1) from borrow_record where reader_id = #{readerId} and status = 'OVERDUE'")
    int countOverdueByReader(@Param("readerId") Long readerId);

    @Select("""
            select coalesce(sum(fine), 0) from borrow_record
             where reader_id = #{readerId} and status = 'OVERDUE'
            """)
    BigDecimal sumUnpaidFine(@Param("readerId") Long readerId);

    /**
     * 归还。带 {@code status in ('BORROWED','OVERDUE')} 条件，
     * 并发重复归还时只有一个事务能命中，其余影响行数为 0 —— 这是防"重复归还把库存加爆"的关键。
     *
     * <p>注意：这里必须是 {@code @Update} 而不是 {@code @Select}。
     * 写成 @Select 时 MyBatis 会按查询执行，返回 null，
     * 而本方法声明返回基本类型 int，会直接抛 BindingException，导致还书功能完全不可用。
     */
    @Update("""
            update borrow_record
               set status = 'RETURNED', return_at = #{returnAt}, updated_at = now()
             where id = #{id} and status in ('BORROWED', 'OVERDUE')
            """)
    int markReturned(@Param("id") Long id, @Param("returnAt") LocalDateTime returnAt);

    /**
     * 续借。带 status 条件，避免把已归还的记录"续借"成功（幂等与状态机兜底）。
     */
    @Update("""
            update borrow_record
               set renew_count = renew_count + 1, due_at = #{dueAt}, status = 'BORROWED', updated_at = now()
             where id = #{id} and status in ('BORROWED', 'OVERDUE')
            """)
    int markRenewed(@Param("id") Long id, @Param("dueAt") LocalDateTime dueAt);

    @Update("""
            update borrow_record set status = #{status}, fine = #{fine}, updated_at = now()
             where id = #{id}
            """)
    int updateStatusAndFine(@Param("id") Long id, @Param("status") String status, @Param("fine") BigDecimal fine);

    @Select("select * from borrow_record where reader_id = #{readerId} order by borrow_at desc limit #{limit}")
    List<BorrowRecord> listByReader(@Param("readerId") Long readerId, @Param("limit") int limit);

    /**
     * 逾期扫描：定时任务用。返回状态仍为 BORROWED 但已过应还时间的记录。
     */
    @Select("""
            select * from borrow_record
             where status = 'BORROWED' and due_at < #{now}
             order by due_at asc
             limit #{limit}
            """)
    List<BorrowRecord> findDueBefore(@Param("now") LocalDateTime now, @Param("limit") int limit);

    /** "我的借阅"列表：一次 join 拿到书名与馆藏位置，避免 N+1 查询。 */
    @Select("""
            select br.id as borrow_id, br.book_id as book_id, b.title as title, b.author as author,
                   b.shelf_location as shelf_location, br.borrow_at as borrow_at, br.due_at as due_at,
                   br.renew_count as renew_count, br.status as status, br.fine as fine
              from borrow_record br
              join book b on b.id = br.book_id
             where br.reader_id = #{readerId}
             order by case when br.status in ('BORROWED', 'OVERDUE') then 0 else 1 end, br.borrow_at desc
             limit #{limit}
            """)
    List<LoanRow> listLoanRows(@Param("readerId") Long readerId, @Param("limit") int limit);

    /**
     * 统计某书在指定时间窗口内的借阅次数，供推荐与热门榜使用。
     */
    @Select("""
            select count(1) from borrow_record
             where book_id = #{bookId} and borrow_at >= #{since}
            """)
    int countBorrowedSince(@Param("bookId") Long bookId, @Param("since") LocalDateTime since);
}
