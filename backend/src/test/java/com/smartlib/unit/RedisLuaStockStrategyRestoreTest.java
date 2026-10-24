package com.smartlib.unit;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.RedisScript;
import org.springframework.core.io.ClassPathResource;

import com.smartlib.service.stock.RedisLuaStockStrategy;

/**
 * FIX-06 回归测试：{@code redis-lua} 策略归还时必须把额度回补到 Redis。
 *
 * <p>缺陷背景：归还路径原本只更新数据库，不回补 Redis 预扣池。真机复现（Redis 8.10.2）：
 * 借出 10 册全部归还后数据库 available=10，而 {@code smartlib:stock:{id}} 停在 0，
 * 之后所有借阅都被 Lua 判成"库存不足"，要等 30 分钟 TTL 才自愈。
 *
 * <p>为什么这个测试必须存在：原有 {@code StockConcurrencyTest} 只在 {@code local} profile
 * 下跑（默认策略是 optimistic-db），唯一涉及 redis-lua 的用例测的是"Redis 挂了怎么降级"，
 * 成功路径从来没有断言过 —— 所以这个缺陷能一直存活。
 *
 * <p>这里不打 Spring 容器：构造策略只 set 脚本位置、不加载，因此可以脱离 Redis 运行。
 */
class RedisLuaStockStrategyRestoreTest {

    /** 记录 execute 调用的假模板：不连真实 Redis。 */
    private static class RecordingRedisTemplate extends StringRedisTemplate {
        final List<String> keysSeen = new ArrayList<>();
        final List<Object> argsSeen = new ArrayList<>();
        RedisScript<?> scriptSeen;
        Long returnValue = 10L;
        RuntimeException toThrow;

        @Override
        @SuppressWarnings("unchecked")
        public <T> T execute(RedisScript<T> script, List<String> keys, Object... args) {
            if (toThrow != null) {
                throw toThrow;
            }
            scriptSeen = script;
            keysSeen.addAll(keys);
            argsSeen.addAll(List.of(args));
            return (T) returnValue;
        }
    }

    private static ObjectProvider<StringRedisTemplate> provider(StringRedisTemplate template) {
        return new ObjectProvider<>() {
            @Override
            public StringRedisTemplate getObject() {
                return template;
            }
        };
    }

    private static RedisLuaStockStrategy strategyWith(StringRedisTemplate template) {
        // stockMapper / fallback 在 restore 路径上不参与，传 null 即可（构造器不触碰它们）
        return new RedisLuaStockStrategy(null, provider(template), null, true);
    }

    @Test
    @DisplayName("归还回补：按 bookId 拼 key，并把数量作为参数发给 Lua 脚本")
    void restoreSendsIncrByScriptWithBookKey() {
        RecordingRedisTemplate redis = new RecordingRedisTemplate();
        strategyWith(redis).restore(15L, 1);

        assertEquals(List.of("smartlib:stock:15"), redis.keysSeen, "key 前缀必须与扣减侧一致");
        assertEquals(List.of("1"), redis.argsSeen.stream().map(String::valueOf).toList());
        assertNotNull(redis.scriptSeen, "必须走 Lua 脚本，而不是直接 INCRBY");
    }

    @Test
    @DisplayName("归还回补：数量非正时不做任何操作")
    void restoreSkipsNonPositiveQuantity() {
        RecordingRedisTemplate redis = new RecordingRedisTemplate();
        RedisLuaStockStrategy strategy = strategyWith(redis);

        strategy.restore(15L, 0);
        strategy.restore(15L, -1);

        assertTrue(redis.keysSeen.isEmpty(), "数量非正时不应访问 Redis");
    }

    @Test
    @DisplayName("归还回补：Redis 抛异常时必须吞掉，不能反向影响已经成功的归还")
    void restoreSwallowsRedisFailure() {
        RecordingRedisTemplate redis = new RecordingRedisTemplate();
        redis.toThrow = new IllegalStateException("redis down");

        assertDoesNotThrow(() -> strategyWith(redis).restore(15L, 1));
    }

    @Test
    @DisplayName("回补脚本必须随包发布（路径写错只会在运行期炸，这里提前拦住）")
    void restoreScriptIsPackaged() throws Exception {
        ClassPathResource res = new ClassPathResource("lua/stock_restore.lua");
        assertTrue(res.exists(), "resources/lua/stock_restore.lua 不存在，回补会在运行期失败");

        String body = new String(res.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        assertTrue(body.contains("EXISTS"), "脚本必须先 EXISTS 再 INCRBY，否则会把未预热的 key 创建成错误初值");
        assertTrue(body.contains("INCRBY"), "脚本必须真正回补额度");
    }
}
