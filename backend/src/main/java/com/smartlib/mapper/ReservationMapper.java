package com.smartlib.mapper;

import java.time.LocalDateTime;
import java.util.List;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import com.smartlib.domain.entity.Reservation;

/**
 * 预约队列 Mapper。
 *
 * <p>队列是 FIFO 的，出队依赖 {@code (book_id, status, queue_no)} 上的联合索引：
 * {@link #findQueueHead} 只取队首一条，{@link #maxQueueNo} 用于新预约取号。
 * 取号与插入必须在同一事务内，否则并发预约会出现重号（详见解决方案手册 P8 慢 SQL 案例三）。
 */
public interface ReservationMapper {

    @Insert("""
            insert into reservation(reader_id, book_id, queue_no, status, ready_at, expire_at, created_at, updated_at)
            values(#{readerId}, #{bookId}, #{queueNo}, #{status}, #{readyAt}, #{expireAt}, now(), now())
            """)
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(Reservation reservation);

    @Select("select * from reservation where id = #{id}")
    Reservation findById(@Param("id") Long id);

    @Select("select count(1) from reservation where book_id = #{bookId} and status = 'WAITING'")
    int countWaiting(@Param("bookId") Long bookId);

    @Select("select coalesce(max(queue_no), 0) from reservation where book_id = #{bookId}")
    int maxQueueNo(@Param("bookId") Long bookId);

    @Select("""
            select * from reservation
             where book_id = #{bookId} and status = 'WAITING'
             order by queue_no asc
             limit 1
            """)
    Reservation findQueueHead(@Param("bookId") Long bookId);

    @Select("""
            select count(1) from reservation
             where reader_id = #{readerId} and book_id = #{bookId} and status in ('WAITING', 'READY')
            """)
    int countActiveByReaderAndBook(@Param("readerId") Long readerId, @Param("bookId") Long bookId);

    @Select("select * from reservation where reader_id = #{readerId} and status in ('WAITING', 'READY') order by created_at desc")
    List<Reservation> listActiveByReader(@Param("readerId") Long readerId);

    @Update("""
            update reservation
               set status = #{status}, ready_at = #{readyAt}, expire_at = #{expireAt}, updated_at = now()
             where id = #{id}
            """)
    int updateStatus(@Param("id") Long id,
                     @Param("status") String status,
                     @Param("readyAt") LocalDateTime readyAt,
                     @Param("expireAt") LocalDateTime expireAt);

    /**
     * 到馆保留超期的预约：定时任务据此释放名额并通知下一位。
     */
    @Select("""
            select * from reservation
             where status = 'READY' and expire_at < #{now}
             order by expire_at asc
             limit #{limit}
            """)
    List<Reservation> findExpiredReady(@Param("now") LocalDateTime now, @Param("limit") int limit);
}
