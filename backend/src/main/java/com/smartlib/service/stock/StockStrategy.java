package com.smartlib.service.stock;

import com.smartlib.domain.dto.Dtos.StockDeductResult;

/**
 * 库存扣减策略（SPI）。
 *
 * <p>整个项目只有一个入口会改动 {@code book_stock.available_copies}，就是本接口的实现。
 * 通过把"如何保证并发正确"抽象成可插拔策略，我们可以在不改业务代码的前提下，
 * 按场景切换方案并用同一套压测脚本对比它们（见 docs/09-多种解决方案对比手册.md P1）。
 *
 * <p><b>所有实现必须遵守的契约：</b>
 * <ol>
 *   <li>扣减语句必须带 {@code available_copies >= quantity} 条件，永不允许把库存扣成负数；</li>
 *   <li>失败必须返回 {@link StockDeductResult#success()} = false 并给出可读原因，不允许抛异常表达业务失败；</li>
 *   <li>成功时必须返回准确的扣减前后数量，供上层写入库存流水；</li>
 *   <li>若实现自行写流水（如 MQ 策略在自己的事务里完成原子扣减+记账），
 *       必须覆盖 {@link #writesOwnLedger()} 返回 true。</li>
 * </ol>
 */
public interface StockStrategy {

    /** 策略名，与配置项 {@code smartlib.stock.strategy} 的取值一致，会落库到 stock_ledger.strategy。 */
    String name();

    /**
     * 尝试扣减库存。
     *
     * @param bookId   书目 id
     * @param quantity 扣减数量，必须为正数
     * @param bizNo    业务流水号，全局唯一，用于库存流水去重与对账
     */
    StockDeductResult tryDeduct(long bookId, int quantity, String bizNo);

    /**
     * 归还（或扣减失败后的补偿）时，把额度还回该策略自己的加速结构。
     *
     * <p><b>为什么接口必须有这个方法</b>：扣减与归还是一对。只有扣减没有归还，
     * 任何"把库存计数放在数据库之外"的策略都会漂移。实测缺陷（FIX-06）：
     * {@code redis-lua} 策略借出 10 册再全部归还后，数据库回到 10，
     * 但 Redis 的 {@code smartlib:stock:{id}} 停在 0，之后所有借阅都被 Lua
     * 判成"库存不足"—— 账没算错（对账仍一致、零超卖），但<b>功能不可用</b>，
     * 且要等 30 分钟 TTL 过期才自愈。降级开关也救不了：Redis 没有故障，
     * 它只是如实回答"库存不足"，被当成合法业务拒绝。
     *
     * <p>默认空实现：以数据库为唯一事实来源的策略（条件更新、悲观锁、
     * 乐观锁重试、本地锁、队列串行化）本来就不需要额外补偿。
     *
     * <p><b>实现约束</b>：回补失败绝不能抛异常 —— 归还本身已经成功，
     * 回补失败最多让缓存额度暂时偏低，由 TTL 与对账任务兜底。
     *
     * @param bookId   书目 id
     * @param quantity 回补数量，必须为正数
     */
    default void restore(long bookId, int quantity) {
        // 数据库即事实来源，无需额外动作
    }

    /**
     * 该策略是否已在自己掌控的事务里写入了库存流水。
     * 默认 false：由 {@code StockService} 在同一个事务里统一写流水。
     */
    default boolean writesOwnLedger() {
        return false;
    }

    /** 是否为多实例安全的策略。单机锁在多实例部署下会失效，压测与评审时会强调这一点。 */
    default boolean multiInstanceSafe() {
        return true;
    }
}
