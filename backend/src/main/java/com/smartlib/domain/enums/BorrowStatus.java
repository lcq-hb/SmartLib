package com.smartlib.domain.enums;

/**
 * 借阅状态：BORROWED 在借 / RETURNED 已归还 / OVERDUE 逾期 / LOST 遗失。
 *
 * <p>本枚举由 scripts/tools/gen_entities.py 生成。
 */
public enum BorrowStatus {
    BORROWED,
    RETURNED,
    OVERDUE,
    LOST;
}
