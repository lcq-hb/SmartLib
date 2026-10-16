package com.smartlib.domain.enums;

/**
 * 预约状态：WAITING 排队中 / READY 到馆保留中 / FULFILLED 已借出 / EXPIRED 过期 / CANCELLED 已取消。
 *
 * <p>本枚举由 scripts/tools/gen_entities.py 生成。
 */
public enum ReservationStatus {
    WAITING,
    READY,
    FULFILLED,
    EXPIRED,
    CANCELLED;
}
