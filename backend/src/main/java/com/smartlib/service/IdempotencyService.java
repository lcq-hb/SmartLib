package com.smartlib.service;

import java.util.function.Supplier;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;

import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;
import com.smartlib.common.Json;
import com.smartlib.domain.entity.IdempotentRecord;
import com.smartlib.mapper.IdempotentRecordMapper;

/**
 * 幂等服务 —— 保证"重复提交只生效一次"。
 *
 * <p><b>为什么这张表必须落在数据库里，而不是只用 Redis</b>：
 * Redis 的 SETNX 能挡住绝大多数重复请求，但它与业务数据在两个不同的存储上，
 * 一旦 Redis 丢数据（重启未持久化、主从切换丢失写入），幂等保证就消失了，
 * 而"借书重复扣库存"是不可接受的。把幂等记录与业务数据放在同一个数据库、
 * 甚至同一个事务里，才能做到"要么都成功、要么都回滚"，见 ADR-009。
 * Redis 可以做前置的快速拦截（性能优化），但最终裁决必须是数据库。
 *
 * <p><b>实现要点：靠唯一索引而不是"先查后插"</b>。
 * 先查后插在并发下必然出现竞态（两个请求同时查不到，然后都去执行）；
 * 这里直接尝试 INSERT，让唯一索引来裁决，把并发正确性交给数据库。
 *
 * <p><b>事务要求</b>：本方法必须在业务事务内被调用。
 * 这样"幂等占位 → 业务执行 → 回填结果"是同一个事务，
 * 业务失败回滚时幂等记录也一起回滚，用户重试不会被误判成"已处理过"。
 */
@Service
public class IdempotencyService {

    private static final Logger log = LoggerFactory.getLogger(IdempotencyService.class);

    private final IdempotentRecordMapper mapper;

    public IdempotencyService(IdempotentRecordMapper mapper) {
        this.mapper = mapper;
    }

    /**
     * 幂等执行包装。
     *
     * @param idemKey 幂等键，由调用方（客户端）生成，通常是一个 UUID
     * @param bizType 业务类型，便于排查"这个幂等键是哪个接口产生的"
     * @param type    结果类型，用于反序列化复用历史结果
     * @param action  真正的业务逻辑
     */
    public <T> Outcome<T> execute(String idemKey, String bizType, Class<T> type, Supplier<T> action) {
        if (idemKey == null || idemKey.isBlank()) {
            throw new BizException(ErrorCode.IDEM_KEY_REQUIRED);
        }
        if (idemKey.length() > 128) {
            throw new BizException(ErrorCode.PARAM_INVALID, "Idempotency-Key 长度不能超过 128");
        }

        IdempotentRecord placeholder = new IdempotentRecord();
        placeholder.setIdemKey(idemKey);
        placeholder.setBizType(bizType);
        placeholder.setResultJson(null);

        try {
            mapper.insert(placeholder);
            log.debug("幂等占位成功 key={} bizType={}", idemKey, bizType);
        } catch (DuplicateKeyException e) {
            // 走到这里说明这个幂等键已经被用过：要么已经完成，要么正在处理中
            return replay(idemKey, type);
        }

        T result = action.get();
        // 回填结果（带 result_json is null 条件，避免覆盖别人的结果）
        mapper.completeResult(idemKey, Json.toJson(result));
        return new Outcome<>(result, false);
    }

    /**
     * 重放：读取历史结果返回。
     *
     * <p>如果历史记录存在但结果还是空的，说明另一个请求正在处理同一笔业务。
     * 这里做短暂自旋等待（最多约 200ms），因为绝大多数情况下对方很快就会提交；
     * 等待后仍为空则返回"处理中"，让客户端稍后重试，而不是给它一个错误的成功响应。
     */
    private <T> Outcome<T> replay(String idemKey, Class<T> type) {
        for (int i = 0; i < 10; i++) {
            IdempotentRecord existing = mapper.findByKey(idemKey);
            if (existing != null && existing.getResultJson() != null && !existing.getResultJson().isBlank()) {
                log.info("命中幂等重放 key={} bizType={}", idemKey, existing.getBizType());
                return new Outcome<>(Json.fromJson(existing.getResultJson(), type), true);
            }
            try {
                Thread.sleep(20);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                break;
            }
        }
        log.warn("相同幂等键的请求正在处理中 key={}", idemKey);
        throw new BizException(ErrorCode.IDEM_IN_PROGRESS);
    }

    /** 清理过期幂等记录，由定时任务调用。 */
    public int cleanup(java.time.LocalDateTime before) {
        int deleted = mapper.deleteCreatedBefore(before);
        if (deleted > 0) {
            log.info("清理过期幂等记录 {} 条（早于 {}）", deleted, before);
        }
        return deleted;
    }

    /**
     * 执行结果 + 是否为重放。
     *
     * @param replayed true 表示这次没有真正执行业务，返回的是首次执行的结果
     */
    public record Outcome<T>(T value, boolean replayed) {
    }
}
