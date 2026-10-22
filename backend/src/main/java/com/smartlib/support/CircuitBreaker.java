package com.smartlib.support;

import java.time.Duration;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 熔断器（每个依赖一个独立熔断器）。
 *
 * <p>状态机：
 * <pre>
 *           失败率 >= 阈值(达到最小样本数)
 *   CLOSED ──────────────────────────────▶ OPEN
 *      ▲                                    │ 等待 openDuration
 *      │ 探测成功                            ▼
 *      └──────────── HALF_OPEN ◀────────────┘
 *                       │ 探测失败
 *                       └──────────▶ OPEN(重新计时)
 * </pre>
 *
 * <p><b>与限流的区别</b>（容易混淆的点）：限流保护的是<b>自己</b>不被压垮，主动拒绝流量；
 * 熔断保护的是<b>调用方</b>不被下游拖死，在下游已经不可用时快速失败并走降级，
 * 从而把线程从"等超时"里解放出来。
 *
 * <p><b>为什么 HALF_OPEN 只放行少量探测</b>：如果半开状态直接把流量全放过去，
 * 下游可能刚从故障中恢复就又被打挂（雪崩的二次触发）。这里只放行固定数量的探测请求，
 * 全部成功才回到 CLOSED。
 */
@Component
public class CircuitBreaker {

    private static final Logger log = LoggerFactory.getLogger(CircuitBreaker.class);

    public enum State {
        /** 正常放行。 */
        CLOSED,
        /** 已熔断，直接走降级，不做真实调用。 */
        OPEN,
        /** 半开，放行少量探测请求。 */
        HALF_OPEN
    }

    private final double failureRateThreshold;
    private final int minimumCalls;
    private final Duration openDuration;
    private final int halfOpenProbes;
    private final Map<String, Breaker> breakers = new ConcurrentHashMap<>();

    public CircuitBreaker(
            @Value("${smartlib.circuit-breaker.failure-rate-threshold:0.5}") double failureRateThreshold,
            @Value("${smartlib.circuit-breaker.minimum-calls:20}") int minimumCalls,
            @Value("${smartlib.circuit-breaker.open-duration-seconds:10}") long openDurationSeconds,
            @Value("${smartlib.circuit-breaker.half-open-probes:3}") int halfOpenProbes) {
        this.failureRateThreshold = failureRateThreshold;
        this.minimumCalls = minimumCalls;
        this.openDuration = Duration.ofSeconds(openDurationSeconds);
        this.halfOpenProbes = halfOpenProbes;
    }

    /**
     * 带熔断与降级的调用。
     *
     * @param name     依赖名，如 {@code java-backend-search}、{@code redis}
     * @param action   真实调用
     * @param fallback 降级逻辑；熔断打开或调用失败时执行
     */
    public <T> T call(String name, java.util.function.Supplier<T> action, java.util.function.Supplier<T> fallback) {
        Breaker breaker = breakers.computeIfAbsent(name, k -> new Breaker());
        if (!breaker.allowRequest(halfOpenProbes)) {
            log.debug("熔断器 [{}] 处于 OPEN，直接走降级", name);
            breaker.recordShortCircuited();
            return fallback.get();
        }
        try {
            T result = action.get();
            breaker.recordSuccess();
            return result;
        } catch (Exception e) {
            breaker.recordFailure(failureRateThreshold, minimumCalls, openDuration);
            log.warn("依赖 [{}] 调用失败，走降级：{}", name, e.toString());
            return fallback.get();
        }
    }

    public State stateOf(String name) {
        Breaker breaker = breakers.get(name);
        return breaker == null ? State.CLOSED : breaker.currentState();
    }

    /** 供监控与压测重置用。 */
    public void reset(String name) {
        Breaker breaker = breakers.get(name);
        if (breaker != null) {
            breaker.reset();
        }
    }

    public Map<String, State> allStates() {
        Map<String, State> result = new ConcurrentHashMap<>();
        breakers.forEach((k, v) -> result.put(k, v.currentState()));
        return result;
    }

    /**
     * 单个熔断器。滑动窗口用固定长度的双端队列实现，
     * 只保留最近 {@code windowSize} 次调用的结果，无需引入指标库。
     */
    private static final class Breaker {

        private final Deque<Boolean> window = new ArrayDeque<>();
        private int windowSize = 20;
        private State state = State.CLOSED;
        private long openedAtNanos;
        private int halfOpenRemaining;
        private final AtomicLong shortCircuited = new AtomicLong();

        synchronized boolean allowRequest(int halfOpenProbes) {
            long now = System.nanoTime();
            if (state == State.OPEN) {
                if (now - openedAtNanos >= openDurationNanos) {
                    state = State.HALF_OPEN;
                    halfOpenRemaining = halfOpenProbes;
                    log.info("熔断器进入 HALF_OPEN，放行 {} 个探测请求", halfOpenProbes);
                } else {
                    return false;
                }
            }
            if (state == State.HALF_OPEN) {
                if (halfOpenRemaining <= 0) {
                    return false;
                }
                halfOpenRemaining--;
            }
            return true;
        }

        private long openDurationNanos = Duration.ofSeconds(10).toNanos();

        synchronized void recordSuccess() {
            if (state == State.HALF_OPEN) {
                state = State.CLOSED;
                window.clear();
                log.info("熔断器探测成功，恢复 CLOSED");
                return;
            }
            push(true);
        }

        synchronized void recordFailure(double threshold, int minimumCalls, Duration openDur) {
            this.openDurationNanos = openDur.toNanos();
            this.windowSize = Math.max(minimumCalls * 2, 20);
            if (state == State.HALF_OPEN) {
                trip();
                return;
            }
            push(false);
            if (window.size() >= minimumCalls) {
                long failures = window.stream().filter(ok -> !ok).count();
                double rate = (double) failures / window.size();
                if (rate >= threshold) {
                    trip();
                }
            }
        }

        void recordShortCircuited() {
            shortCircuited.incrementAndGet();
        }

        private void trip() {
            state = State.OPEN;
            openedAtNanos = System.nanoTime();
            window.clear();
            log.warn("熔断器触发 -> OPEN，{} 秒后进入半开探测", openDurationNanos / 1_000_000_000L);
        }

        synchronized void reset() {
            state = State.CLOSED;
            window.clear();
        }

        synchronized State currentState() {
            // 读取时也做一次超时转换，否则 OPEN 状态会一直显示到下一次调用
            if (state == State.OPEN && System.nanoTime() - openedAtNanos >= openDurationNanos) {
                state = State.HALF_OPEN;
                halfOpenRemaining = 1;
            }
            return state;
        }

        private void push(boolean success) {
            if (window.size() >= windowSize) {
                window.pollFirst();
            }
            window.addLast(success);
        }
    }
}
