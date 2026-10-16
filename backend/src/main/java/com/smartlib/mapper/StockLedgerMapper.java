package com.smartlib.mapper;

import java.util.List;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import com.smartlib.domain.entity.StockLedger;

/**
 * 库存流水 Mapper。
 *
 * <p>不变式：对任一 book_id，{@code available_copies = total_copies + sum(delta)}。
 * {@link #findViolations} 就是把这个不变式写成 SQL —— 它是发现"绕过 StockService 的
 * 非法写路径"的最后一道防线，也是故障复盘后新增的永久修复项之一（FIX-02）。
 */
public interface StockLedgerMapper {

    @Insert("""
            insert into stock_ledger(book_id, op_type, delta, before_qty, after_qty,
                                     biz_no, strategy, trace_id, created_at)
            values(#{bookId}, #{opType}, #{delta}, #{beforeQty}, #{afterQty},
                   #{bizNo}, #{strategy}, #{traceId}, now())
            """)
    int insert(StockLedger ledger);

    /** 幂等兜底：同一 bizNo 只能有一条流水，重复插入会因唯一索引失败。 */
    @Select("select count(1) from stock_ledger where biz_no = #{bizNo}")
    int countByBizNo(@Param("bizNo") String bizNo);

    @Select("select coalesce(sum(delta), 0) from stock_ledger where book_id = #{bookId}")
    int sumDelta(@Param("bookId") Long bookId);

    @Select("select * from stock_ledger where book_id = #{bookId} order by id desc limit #{limit}")
    List<StockLedger> findRecent(@Param("bookId") Long bookId, @Param("limit") int limit);

    /**
     * 对账：找出库存与流水不守恒的书目。
     * 正常情况下必须返回空集合；非空即代表存在漏记流水或非法直改库存的代码路径。
     */
    @Select("""
            select s.book_id
              from book_stock s
              left join (
                select book_id, sum(delta) as d from stock_ledger group by book_id
              ) l on l.book_id = s.book_id
             where s.available_copies <> s.total_copies + coalesce(l.d, 0)
            """)
    List<Long> findViolations();
}
