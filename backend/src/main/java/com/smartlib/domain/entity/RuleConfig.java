package com.smartlib.domain.entity;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 借阅规则配置实体，改规则不发版。
 *
 * <p>本类由 scripts/tools/gen_entities.py 生成，请勿手工修改字段：
 * 需要调整字段请修改生成器中的字段清单后重新生成（与
 * backend/src/main/resources/db/schema.sql 一一对应）。
 */
public class RuleConfig {

    private Long id;
    private String ruleKey;
    private String readerType;
    private String ruleValue;
    private String remark;
    private Integer version;
    private LocalDateTime updatedAt;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getRuleKey() {
        return ruleKey;
    }

    public void setRuleKey(String ruleKey) {
        this.ruleKey = ruleKey;
    }

    public String getReaderType() {
        return readerType;
    }

    public void setReaderType(String readerType) {
        this.readerType = readerType;
    }

    public String getRuleValue() {
        return ruleValue;
    }

    public void setRuleValue(String ruleValue) {
        this.ruleValue = ruleValue;
    }

    public String getRemark() {
        return remark;
    }

    public void setRemark(String remark) {
        this.remark = remark;
    }

    public Integer getVersion() {
        return version;
    }

    public void setVersion(Integer version) {
        this.version = version;
    }

    public LocalDateTime getUpdatedAt() {
        return updatedAt;
    }

    public void setUpdatedAt(LocalDateTime updatedAt) {
        this.updatedAt = updatedAt;
    }

    @Override
    public String toString() {
        return "RuleConfig{"
                + "id=" + id + ", "
                + "ruleKey=" + ruleKey + ", "
                + "readerType=" + readerType + ", "
                + "ruleValue=" + ruleValue + ", "
                + "remark=" + remark + ", "
                + "version=" + version + ", "
                + "updatedAt=" + updatedAt + ", "
                + '}';
    }
}
