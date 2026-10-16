package com.smartlib.mapper;

import java.time.LocalDateTime;

import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import com.smartlib.domain.entity.IdempotentRecord;

/**
 * 幂等记录 Mapper。
 *
 * <p>幂等靠"唯一索引 + 冲突感知"实现，而不是先查后插：
 * 先 {@link #insert} 抢占，唯一索引冲突说明已有请求在处理或已完成，
 * 这样就避免了"两个并发请求同时查不到、同时执行"的经典竞态。
 */
public interface IdempotentRecordMapper {

    @Insert("""
            insert into idempotent_record(idem_key, biz_type, result_json, created_at)
            values(#{idemKey}, #{bizType}, #{resultJson}, now())
            """)
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(IdempotentRecord record);

    @Select("select * from idempotent_record where idem_key = #{idemKey}")
    IdempotentRecord findByKey(@Param("idemKey") String idemKey);

    /**
     * 回填结果。带 {@code result_json is null} 条件：只有占位行才能被本请求写入，
     * 防止后来的请求覆盖先前的真实结果。
     */
    @Update("""
            update idempotent_record set result_json = #{resultJson}
             where idem_key = #{idemKey} and result_json is null
            """)
    int completeResult(@Param("idemKey") String idemKey, @Param("resultJson") String resultJson);

    /** 清理过期幂等记录，避免表无限膨胀（定时任务调用）。 */
    @Delete("delete from idempotent_record where created_at < #{before}")
    int deleteCreatedBefore(@Param("before") LocalDateTime before);
}
