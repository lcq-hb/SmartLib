package com.smartlib.domain.enums;

/**
 * 库存操作类型：DEDUCT 扣减 / RELEASE 归还释放 / ROLLBACK 回滚补偿 / INIT 初始化。
 *
 * <p>本枚举由 scripts/tools/gen_entities.py 生成。
 */
public enum StockOpType {
    DEDUCT,
    RELEASE,
    ROLLBACK,
    INIT;
}
