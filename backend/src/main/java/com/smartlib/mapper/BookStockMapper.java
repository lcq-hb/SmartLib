package com.smartlib.mapper;

import java.util.List;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import com.smartlib.domain.entity.BookStock;

/**
 * 库存 Mapper。
 *
 * <p><b>这是全项目最关键的一组 SQL。</b>所有扣减语句都必须带
 * {@code available_copies >= #{n}} 条件，任何策略都不允许出现无约束的 UPDATE。
 * 各方法对应 docs/09-多种解决方案对比手册.md 中的不同方案：
 * <ul>
 *   <li>{@link #deductConditional} —— S2 DB 条件更新（默认方案）</li>
 *   <li>{@link #selectForUpdate} —— S3 悲观锁（需在事务中，锁到提交才释放）</li>
 *   <li>{@link #deductWithVersion} —— S4 乐观锁 CAS</li>
 * </ul>
 */
public interface BookStockMapper {

    @Select("select * from book_stock where book_id = #{bookId}")
    BookStock findByBookId(@Param("bookId") Long bookId);

    @Select("select * from book_stock")
    List<BookStock> findAll();

    @Insert("""
            insert into book_stock(book_id, total_copies, available_copies, locked_copies, version, updated_at)
            values(#{bookId}, #{totalCopies}, #{availableCopies}, #{lockedCopies}, #{version}, #{updatedAt})
            """)
    int insert(BookStock stock);

    /**
     * S2：条件更新扣减。影响行数为 0 表示库存不足，天然防超卖，无需显式加锁。
     * version 一并自增，保证乐观锁策略读到的 version 单调递增，不会读到"旧但相同"的值。
     */
    @Update("""
            update book_stock
               set available_copies = available_copies - #{n},
                   version = version + 1,
                   updated_at = now()
             where book_id = #{bookId}
               and available_copies >= #{n}
            """)
    int deductConditional(@Param("bookId") Long bookId, @Param("n") int n);

    /**
     * S3：悲观锁。必须在事务中调用，锁到事务提交才释放，
     * 因此加锁与更新之间不能插入 RPC、发送消息等耗时操作，否则会放大锁等待。
     */
    @Select("select * from book_stock where book_id = #{bookId} for update")
    BookStock selectForUpdate(@Param("bookId") Long bookId);

    /**
     * S4：乐观锁 CAS。version 不匹配或库存不足都返回 0，由调用方决定是否重试。
     */
    @Update("""
            update book_stock
               set available_copies = available_copies - #{n},
                   version = version + 1,
                   updated_at = now()
             where book_id = #{bookId}
               and version = #{version}
               and available_copies >= #{n}
            """)
    int deductWithVersion(@Param("bookId") Long bookId, @Param("n") int n, @Param("version") int version);

    /**
     * 归还/回补。上限为总复本数，防止把库存加爆（对账 SQL 会校验这一点）。
     */
    @Update("""
            update book_stock
               set available_copies = available_copies + #{n},
                   version = version + 1,
                   updated_at = now()
             where book_id = #{bookId}
               and available_copies + #{n} <= total_copies
            """)
    int release(@Param("bookId") Long bookId, @Param("n") int n);

    @Update("""
            update book_stock set locked_copies = locked_copies + #{n}, updated_at = now()
             where book_id = #{bookId}
            """)
    int increaseLocked(@Param("bookId") Long bookId, @Param("n") int n);

    @Update("""
            update book_stock set locked_copies = locked_copies - #{n}, updated_at = now()
             where book_id = #{bookId} and locked_copies >= #{n}
            """)
    int decreaseLocked(@Param("bookId") Long bookId, @Param("n") int n);

    /**
     * 同步 book 表的冗余 total_copies（见 schema.sql 对该反范式字段的说明）。
     */
    @Update("update book set total_copies = #{totalCopies}, updated_at = now() where id = #{bookId}")
    int syncBookTotalCopies(@Param("bookId") Long bookId, @Param("totalCopies") int totalCopies);

    /**
     * 采编入库：总复本数与可借数同时增加。
     * 注意扣减/归还<b>不会</b>改动 total_copies，只有增减复本才改，
     * 否则 available = total + sum(delta) 这个对账不变式就不成立了。
     */
    @Update("""
            update book_stock
               set total_copies = total_copies + #{n},
                   available_copies = available_copies + #{n},
                   version = version + 1,
                   updated_at = now()
             where book_id = #{bookId}
            """)
    int addCopies(@Param("bookId") Long bookId, @Param("n") int n);

    /**
     * 对账用：book 表的冗余 total_copies 与 book_stock 不一致的书目 id。
     * 这个查询应当永远返回空集合，非空即说明存在绕过 StockService 的写路径。
     */
    @Select("""
            select b.id from book b
             join book_stock s on s.book_id = b.id
            where b.total_copies <> s.total_copies
            """)
    List<Long> findTotalCopiesMismatch();

    /**
     * 对账用：可用库存为负的异常数据（理论上不可能出现，出现即说明有写路径漏了条件校验）。
     */
    @Select("select book_id from book_stock where available_copies < 0")
    List<Long> findNegativeStock();
}
