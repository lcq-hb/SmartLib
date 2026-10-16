package com.smartlib.domain.entity;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 幂等记录实体，保证写操作重复提交只生效一次。
 *
 * <p>本类由 scripts/tools/gen_entities.py 生成，请勿手工修改字段：
 * 需要调整字段请修改生成器中的字段清单后重新生成（与
 * backend/src/main/resources/db/schema.sql 一一对应）。
 */
public class IdempotentRecord {

    private Long id;
    private String idemKey;
    private String bizType;
    private String resultJson;
    private LocalDateTime createdAt;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getIdemKey() {
        return idemKey;
    }

    public void setIdemKey(String idemKey) {
        this.idemKey = idemKey;
    }

    public String getBizType() {
        return bizType;
    }

    public void setBizType(String bizType) {
        this.bizType = bizType;
    }

    public String getResultJson() {
        return resultJson;
    }

    public void setResultJson(String resultJson) {
        this.resultJson = resultJson;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(LocalDateTime createdAt) {
        this.createdAt = createdAt;
    }

    @Override
    public String toString() {
        return "IdempotentRecord{"
                + "id=" + id + ", "
                + "idemKey=" + idemKey + ", "
                + "bizType=" + bizType + ", "
                + "resultJson=" + resultJson + ", "
                + "createdAt=" + createdAt + ", "
                + '}';
    }
}
