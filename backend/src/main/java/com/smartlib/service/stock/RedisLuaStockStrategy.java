package com.smartlib.service.stock;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.ClassPathResource;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.stereotype.Component;

import com.smartlib.domain.dto.Dtos.StockDeductResult;
import com.smartlib.domain.entity.BookStock;
import com.smartlib.mapper.BookStockMapper;

/**
 * S5：Redis + Lua 原子预扣（生产默认，Redis 不可用时自动降级）。
 *
 * <p><b>为什么要 Redis 挡在前面：</b>热门书开抢时，几千个请求会同时打到同一行数据库记录上，
 * 数据库行锁把它们串行化，连接池很快被占满。把库存计数放到 Redis，
 * 用 Lua 脚本保证"判断 + 扣减"的原子性，绝大多数"库存不足"的请求在内存层就被挡掉，
 * 只有真正抢到的少量请求才会去写数据库。
 *
 * <p><b>本实现的一致性是"预扣 + 立即落库 + 失败回滚"，而不是纯异步落库。</b>
 * 原因：纯异步方案在"Redis 扣成功、数据库还没落"的时间窗里，一旦进程崩溃就会丢单，
 * 需要额外的对账与补偿链路。这里把 Redis 当作<b>流量闸门</b>，
 * 真正的账仍以数据库条件更新为准，因此即使 Redis 与数据库之间的状态出现偏差，
 * 也不可能超卖 —— 数据库那一层的 {@code available_copies >= n} 仍在兜底。
 * 纯异步版（Redis 预扣 + MQ 落库 + 定时对账补偿）的取舍见 docs/09 手册 P1 节。
 *
 * <p><b>降级设计（重要）：</b>Redis 任何异常都不向上抛，而是记日志 + 计数 +
 * 转交 {@link OptimisticDbStockStrategy} 执行。<b>降级后超卖数仍然为 0</b>，
 * 因为兜底策略本身就带条件判断。这条路径是本项目"单点故障不影响正确性"的关键证据，
 * 对应的演练是 DRILL-002（停掉 Redis 观察降级）。
 */
@Component
public class RedisLuaStockStrategy implements StockStrategy {

    public static final String NAME = "redis-lua";

    private static final Logger log = LoggerFactory.getLogger(RedisLuaStockStrategy.class);

    /** key 设计：smartlib:stock:{bookId}，带上业务前缀便于与其它业务隔离和按前缀清理。 */
    private static final String KEY_PREFIX = "smartlib:stock:";
    private static final Duration WARM_UP_TTL = Duration.ofMinutes(30);

    /** Lua 返回值约定，与 resources/lua/stock_deduct.lua 保持一致。 */
    private static final long KEY_MISS = -1L;
    private static final long NOT_ENOUGH = -2L;

    private final BookStockMapper stockMapper;
    private final ObjectProvider<StringRedisTemplate> redisProvider;
    private final OptimisticDbStockStrategy fallback;
    private final DefaultRedisScript<Long> deductScript;
    private final boolean degradeOnRedisFailure;

    private final AtomicLong luaSuccess = new AtomicLong();
    private final AtomicLong degraded = new AtomicLong();

    /**
     * 连续失败计数与短路截止时间。
     *
     * <p><b>为什么必须有短路</b>：压测实测发现，Redis 不可用时若每个请求都去尝试连接，
     * 借阅接口 P99 会从 38ms 恶化到 621ms（≈ Lettuce 的 500ms 连接超时）。
     * 也就是说"缓存故障"会把一个 40ms 的接口变成 600ms 的接口 —— 这正是
     * 缓存故障演变成业务故障的典型路径。加上短路后，只有前几次请求付这个代价，
     * 之后的请求直接走数据库兜底，P99 回到与 {@code optimistic-db} 同一量级。
     *
     * <p>与 {@code RedisL2Cache} 里的短路是同一套思路，两处都实现是因为它们的
     * 访问模式不同（读缓存 vs 写扣减），共用一个组件反而会引入不必要的耦合。
     */
    private static final int FAILURE_THRESHOLD = 3;
    private static final long SHORT_CIRCUIT_NANOS = Duration.ofSeconds(10).toNanos();

    private volatile int consecutiveFailures;
    private volatile long shortCircuitUntilNanos;
    private final AtomicLong shortCircuited = new AtomicLong();

    public RedisLuaStockStrategy(BookStockMapper stockMapper,
                                 ObjectProvider<StringRedisTemplate> redisProvider,
                                 OptimisticDbStockStrategy fallback,
                                 @Value("${smartlib.stock.degrade-on-redis-failure:true}") boolean degradeOnRedisFailure) {
        this.stockMapper = stockMapper;
        this.redisProvider = redisProvider;
        this.fallback = fallback;
        this.degradeOnRedisFailure = degradeOnRedisFailure;
        this.deductScript = new DefaultRedisScript<>();
        this.deductScript.setLocation(new ClassPathResource("lua/stock_deduct.lua"));
        this.deductScript.setResultType(Long.class);
    }

    @Override
    public String name() {
        return NAME;
    }

    /** 已因 Redis 不可用而降级的次数，供 /actuator 指标与演练观测使用。 */
    public long degradedCount() {
        return degraded.get();
    }

    public long luaSuccessCount() {
        return luaSuccess.get();
    }

    @Override
    public StockDeductResult tryDeduct(long bookId, int quantity, String bizNo) {
        // 短路期内直接走兜底：不再尝试连接 Redis，避免每个请求都承担一次连接超时
        if (System.nanoTime() < shortCircuitUntilNanos) {
            shortCircuited.incrementAndGet();
            return degrade(bookId, quantity, bizNo, "Redis 处于短路期");
        }

        StringRedisTemplate redis = redisProvider.getIfAvailable();
        if (redis == null) {
            return degrade(bookId, quantity, bizNo, "RedisTemplate 未装配");
        }

        String key = KEY_PREFIX + bookId;
        try {
            Long remain = redis.execute(deductScript, List.of(key), String.valueOf(quantity));

            if (remain != null && remain == KEY_MISS) {
                // 缓存未预热：从数据库读一次真实库存写回 Redis，然后重试一次
                if (!warmUp(redis, key, bookId)) {
                    return degrade(bookId, quantity, bizNo, "库存缓存预热失败");
                }
                remain = redis.execute(deductScript, List.of(key), String.valueOf(quantity));
            }

            if (remain == null) {
                return degrade(bookId, quantity, bizNo, "Lua 脚本返回空结果");
            }
            if (remain == NOT_ENOUGH) {
                // 内存层已判定不足，无需访问数据库，这正是 Redis 挡量的价值所在
                onRedisSuccess();
                return StockDeductResult.fail(NAME, "库存不足", 0);
            }

            // 内存层扣减成功，立刻用数据库条件更新落账。
            // 数据库才是唯一事实源：如果这里失败（例如 Redis 计数与数据库不一致），必须把 Redis 加回去。
            BookStock stock = stockMapper.findByBookId(bookId);
            int beforeQty = stock == null ? 0 : stock.getAvailableCopies();
            if (stock == null || stockMapper.deductConditional(bookId, quantity) == 0) {
                rollbackRedis(redis, key, quantity);
                onRedisSuccess();
                return StockDeductResult.fail(NAME, "库存不足", beforeQty);
            }

            onRedisSuccess();
            luaSuccess.incrementAndGet();
            return StockDeductResult.ok(NAME, beforeQty, beforeQty - quantity);
        } catch (Exception e) {
            // 关键：不向上抛异常。缓存是加速层，不能因为缓存故障就让借书功能不可用。
            onRedisFailure(e);
            return degrade(bookId, quantity, bizNo, e.getClass().getSimpleName());
        }
    }

    private void onRedisSuccess() {
        if (consecutiveFailures > 0) {
            log.info("Redis 已恢复，退出短路状态");
        }
        consecutiveFailures = 0;
    }

    private void onRedisFailure(Exception e) {
        int failures = ++consecutiveFailures;
        if (failures >= FAILURE_THRESHOLD) {
            shortCircuitUntilNanos = System.nanoTime() + SHORT_CIRCUIT_NANOS;
            log.warn("Redis 连续失败 {} 次，进入 {} 秒短路期（期间直接走数据库兜底，避免每请求一次连接超时）：{}",
                    failures, SHORT_CIRCUIT_NANOS / 1_000_000_000L, e.toString());
        } else {
            log.warn("Redis 预扣失败（第 {} 次），降级为数据库条件更新 bookId={} cause={}",
                    failures, e.toString());
        }
    }

    /** 短路期间被直接兜底掉的请求数，用于观测与演练复盘。 */
    public long shortCircuitedCount() {
        return shortCircuited.get();
    }

    /** Redis 是否处于短路状态。 */
    public boolean shortCircuited() {
        return System.nanoTime() < shortCircuitUntilNanos;
    }

    private StockDeductResult degrade(long bookId, int quantity, String bizNo, String reason) {
        if (!degradeOnRedisFailure) {
            // 生产上确实可以选择"宁可不服务也不降级"，但那样 Redis 就成了单点，
            // 与"单点故障不影响可用性"的目标冲突，因此默认关闭这个行为。
            return StockDeductResult.fail(NAME, "缓存服务不可用：" + reason, 0);
        }
        degraded.incrementAndGet();
        StockDeductResult result = fallback.tryDeduct(bookId, quantity, bizNo);
        // 把降级事实写进策略名，压测与对账时能一眼看出这批流水来自降级路径
        return new StockDeductResult(result.success(), NAME + "->" + fallback.name(),
                result.reason(), result.beforeQty(), result.afterQty(), false);
    }

    private boolean warmUp(StringRedisTemplate redis, String key, long bookId) {
        BookStock stock = stockMapper.findByBookId(bookId);
        if (stock == null) {
            return false;
        }
        redis.opsForValue().set(key, String.valueOf(stock.getAvailableCopies()), WARM_UP_TTL);
        return true;
    }

    private void rollbackRedis(StringRedisTemplate redis, String key, int quantity) {
        try {
            redis.opsForValue().increment(key, quantity);
        } catch (Exception e) {
            // 回滚失败也不能抛：下一轮预热会以数据库为准覆盖这个 key，
            // 兜底路径（数据库条件更新）仍然保证不会超卖。
            log.warn("Redis 预扣回滚失败 key={} qty={}", key, quantity, e);
        }
    }

    /** 供发布/对账使用：让某个书目的缓存计数直接失效，下次请求从数据库重新预热。 */
    public void invalidate(long bookId) {
        StringRedisTemplate redis = redisProvider.getIfAvailable();
        if (redis == null) {
            return;
        }
        try {
            redis.delete(KEY_PREFIX + bookId);
        } catch (Exception e) {
            log.debug("失效 Redis 库存缓存失败 bookId={}", bookId, e);
        }
    }
}
