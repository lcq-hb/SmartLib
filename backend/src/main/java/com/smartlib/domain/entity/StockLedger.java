package com.smartlib.domain.entity;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 库存流水实体，审计与对账的唯一依据。
 *
 * <p>本类由 scripts/tools/gen_entities.py 生成，请勿手工修改字段：
 * 需要调整字段请修改生成器中的字段清单后重新生成（与
 * backend/src/main/resources/db/schema.sql 一一对应）。
 */
public class StockLedger {

    private Long id;
    private Long bookId;
    private String opType;
    private Integer delta;
    private Integer beforeQty;
    private Integer afterQty;
    private String bizNo;
    private String strategy;
    private String traceId;
    private LocalDateTime createdAt;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public Long getBookId() {
        return bookId;
    }

    public void setBookId(Long bookId) {
        this.bookId = bookId;
    }

    public String getOpType() {
        return opType;
    }

    public void setOpType(String opType) {
        this.opType = opType;
    }

    public Integer getDelta() {
        return delta;
    }

    public void setDelta(Integer delta) {
        this.delta = delta;
    }

    public Integer getBeforeQty() {
        return beforeQty;
    }

    public void setBeforeQty(Integer beforeQty) {
        this.beforeQty = beforeQty;
    }

    public Integer getAfterQty() {
        return afterQty;
    }

    public void setAfterQty(Integer afterQty) {
        this.afterQty = afterQty;
    }

    public String getBizNo() {
        return bizNo;
    }

    public void setBizNo(String bizNo) {
        this.bizNo = bizNo;
    }

    public String getStrategy() {
        return strategy;
    }

    public void setStrategy(String strategy) {
        this.strategy = strategy;
    }

    public String getTraceId() {
        return traceId;
    }

    public void setTraceId(String traceId) {
        this.traceId = traceId;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(LocalDateTime createdAt) {
        this.createdAt = createdAt;
    }

    @Override
    public String toString() {
        return "StockLedger{"
                + "id=" + id + ", "
                + "bookId=" + bookId + ", "
                + "opType=" + opType + ", "
                + "delta=" + delta + ", "
                + "beforeQty=" + beforeQty + ", "
                + "afterQty=" + afterQty + ", "
                + "bizNo=" + bizNo + ", "
                + "strategy=" + strategy + ", "
                + "traceId=" + traceId + ", "
                + "createdAt=" + createdAt + ", "
                + '}';
    }
}
