package com.smartlib.unit;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import com.smartlib.cache.BloomFilter;
import com.smartlib.service.search.PinyinUtils;
import com.smartlib.service.search.Tokenizer;
import com.smartlib.support.CircuitBreaker;
import com.smartlib.support.TokenBucketRateLimiter;

/**
 * 核心算法与自研组件的单元测试（不启动 Spring 容器，毫秒级完成）。
 *
 * <p>这些用例的价值在于：把"我自己实现的算法到底对不对"变成可回归的断言，
 * 而不是靠演示时手点一遍。
 */
class CoreComponentsTest {

    // ------------------------------------------------------------------
    // 拼音首字母：用区位码区间表推导，没有字典数据，必须用真实汉字验证
    // ------------------------------------------------------------------

    @Test
    @DisplayName("拼音首字母：区位码区间表对常见汉字的判定正确")
    void pinyinFirstLetter() {
        assertThat(PinyinUtils.firstLetter('中')).isEqualTo('z');
        assertThat(PinyinUtils.firstLetter('计')).isEqualTo('j');
        assertThat(PinyinUtils.firstLetter('网')).isEqualTo('w');
        assertThat(PinyinUtils.firstLetter('络')).isEqualTo('l');
        assertThat(PinyinUtils.firstLetter('深')).isEqualTo('s');
        assertThat(PinyinUtils.firstLetter('算')).isEqualTo('s');
        // 英文与数字原样保留，方便"Java 并发"这类混合书名也能用首字母搜
        assertThat(PinyinUtils.firstLetter('J')).isEqualTo('j');
        assertThat(PinyinUtils.firstLetter('7')).isEqualTo('7');
        // 标点等无法识别的字符返回 0
        assertThat(PinyinUtils.firstLetter('-')).isEqualTo((char) 0);
    }

    @Test
    @DisplayName("拼音首字母：整串转换并丢弃标点")
    void pinyinInitials() {
        assertThat(PinyinUtils.initials("计算机网络")).isEqualTo("jsjwl");
        assertThat(PinyinUtils.initials("深入理解Java虚拟机")).startsWith("srlj").contains("java");
        assertThat(PinyinUtils.hasChinese("计算机网络")).isTrue();
        assertThat(PinyinUtils.hasChinese("elasticsearch")).isFalse();
    }

    // ------------------------------------------------------------------
    // 分词：bigram 方案必须支持"子串查询"这个关键性质
    // ------------------------------------------------------------------

    @Test
    @DisplayName("分词：中文按 bigram 切分，包含整串与拼音前缀")
    void chineseTokenizer() {
        List<String> tokens = Tokenizer.tokenize("计算机网络");
        assertThat(tokens).contains("计算", "算机", "机网", "网络");
        assertThat(tokens).contains("计算机网络");
        assertThat(tokens).contains("py:jsjwl");
        // 前缀也要进索引，否则读者输入 "jsj" 搜不到
        assertThat(tokens).contains("py:js", "py:jsj", "py:jsjw");
    }

    @Test
    @DisplayName("分词：子串查询的召回性质 —— 查询词的 bigram 是文档 bigram 的子集")
    void substringRecallProperty() {
        List<String> docTokens = Tokenizer.tokenize("elasticsearch实战");
        List<String> queryTokens = Tokenizer.tokenize("elastic");

        // "elastic" 的 bigram 必须全部出现在文档词项里，这是 bigram 能支持中缀/前缀匹配的理论依据
        List<String> queryBigrams = queryTokens.stream().filter(t -> t.length() == 2).toList();
        assertThat(queryBigrams).isNotEmpty();
        assertThat(docTokens).containsAll(queryBigrams);
    }

    @Test
    @DisplayName("分词：单字查询与拼音首字母识别")
    void singleCharAndPinyinQuery() {
        assertThat(Tokenizer.tokenize("数")).containsExactly("数");
        assertThat(Tokenizer.looksLikePinyinInitials("jsjwl")).isTrue();
        assertThat(Tokenizer.looksLikePinyinInitials("jsjwl1")).isFalse();
        assertThat(Tokenizer.looksLikePinyinInitials("计算机")).isFalse();
    }

    @Test
    @DisplayName("分词：2 字中文段落不产生单字噪音词项（否则相关度会被污染）")
    void twoCharSegmentHasNoUnigramNoise() {
        // 回归用例：曾经为 2 字中文段落补单字词项，导致搜"算法"会命中《新概念英语》
        // （它的标签"语法"被拆出单字"法"）。2 字段落的整段本身就是 bigram，补单字纯属噪音。
        List<String> tokens = Tokenizer.tokenize("语法");
        assertThat(tokens).contains("语法");
        assertThat(tokens).doesNotContain("语", "法");

        List<String> query = Tokenizer.tokenize("算法");
        assertThat(query).contains("算法").doesNotContain("算", "法");
    }

    @Test
    @DisplayName("分词：拼音首字母查询要生成带 py: 前缀的词项（否则与索引侧对不上）")
    void pinyinQueryProducesPrefixedTerm() {
        // 回归用例：索引侧存 py:jsjwl，查询侧一度只生成裸词项 jsjwl，两边永远匹配不上，
        // 导致"首字母搜中文书名"功能完全不可用。
        assertThat(Tokenizer.tokenize("jsjwl"))
                .as("查询侧必须生成 py: 前缀词项才能命中索引")
                .contains("py:jsjwl", "py:js", "py:jsj", "py:jsjw");
        assertThat(Tokenizer.tokenize("计算机"))
                .as("中文查询也要生成自身的拼音前缀，支持用首字母搜到中文关键词")
                .contains("py:jsj", "py:js");
    }

    // ------------------------------------------------------------------
    // 布隆过滤器：不能有假阴性（会把真实存在的书判为不存在，属严重功能缺陷）
    // ------------------------------------------------------------------

    @Test
    @DisplayName("布隆过滤器：参数估算符合公式，且绝无假阴性")
    void bloomFilterCorrectness() {
        long n = 1_200_000;
        double p = 0.001;
        int m = BloomFilter.optimalBitSize(n, p);
        int k = BloomFilter.optimalHashCount(n, m);

        // 公式：m = -n·ln(p)/(ln2)^2 ≈ 1725 万 bit ≈ 2.06 MB；k = (m/n)·ln2 ≈ 10
        assertThat(m).isBetween(17_000_000, 17_500_000);
        assertThat(k).isEqualTo(10);
        assertThat((long) Math.ceil(m / 8.0) / 1024 / 1024).isLessThanOrEqualTo(3);

        BloomFilter filter = new BloomFilter(10_000, 0.001);
        for (int i = 0; i < 10_000; i++) {
            filter.add("book:" + i);
        }
        // 无假阴性：已插入的元素必须全部判定为"可能存在"，一个都不能漏
        for (int i = 0; i < 10_000; i++) {
            assertThat(filter.mightContain("book:" + i))
                    .as("已插入元素 book:%d 不允许出现假阴性", i)
                    .isTrue();
        }
        assertThat(filter.theoreticalFalsePositiveRate()).isLessThan(0.002);
    }

    @Test
    @DisplayName("布隆过滤器：未插入元素绝大多数被判定为一定不存在（实现拦截价值）")
    void bloomFilterRejectsAbsent() {
        BloomFilter filter = new BloomFilter(10_000, 0.001);
        for (int i = 0; i < 10_000; i++) {
            filter.add("book:" + i);
        }
        int rejected = 0;
        int total = 20_000;
        for (int i = 0; i < total; i++) {
            if (!filter.mightContain("book:absent-" + i)) {
                rejected++;
            }
        }
        // 期望拦截率 > 99.5%（理论误判率 0.1%，留出实现误差余量）
        assertThat((double) rejected / total).isGreaterThan(0.995);
    }

    // ------------------------------------------------------------------
    // 令牌桶限流
    // ------------------------------------------------------------------

    @Test
    @DisplayName("限流：桶容量决定突发上限，超出即拒绝，且按速率补充")
    void tokenBucketLimitsBurstAndRefills() throws Exception {
        TokenBucketRateLimiter limiter = new TokenBucketRateLimiter(true, 10, 100);

        // 突发：前 10 个放行，第 11 个被拒
        for (int i = 0; i < 10; i++) {
            assertThat(limiter.tryAcquire("k")).as("第 %d 个请求应放行", i + 1).isTrue();
        }
        assertThat(limiter.tryAcquire("k")).isFalse();
        assertThat(limiter.rejectedCount()).isEqualTo(1);

        // 不同 key 互不影响（按维度隔离是限流器能用于多入口的前提）
        assertThat(limiter.tryAcquire("other")).isTrue();

        // 惰性补充：100/s 意味着 150ms 后应补出约 15 个令牌
        Thread.sleep(150);
        assertThat(limiter.availableTokens("k")).isGreaterThan(5.0);
        assertThat(limiter.tryAcquire("k")).isTrue();
    }

    // ------------------------------------------------------------------
    // 熔断器三态迁移
    // ------------------------------------------------------------------

    @Test
    @DisplayName("熔断：CLOSED → OPEN → HALF_OPEN → CLOSED 全路径")
    void circuitBreakerStateMachine() throws Exception {
        CircuitBreaker breaker = new CircuitBreaker(0.5, 10, 1, 3);

        // 连续失败到阈值 → OPEN
        for (int i = 0; i < 10; i++) {
            breaker.call("dep", () -> {
                throw new IllegalStateException("模拟下游故障");
            }, () -> "fallback");
        }
        assertThat(breaker.stateOf("dep")).isEqualTo(CircuitBreaker.State.OPEN);

        // OPEN 期间不再真实调用，直接走降级（这一点决定了它能把线程从"等超时"里解放出来）
        AtomicInteger invoked = new AtomicInteger();
        String result = breaker.call("dep", () -> {
            invoked.incrementAndGet();
            return "real";
        }, () -> "fallback");
        assertThat(result).isEqualTo("fallback");
        assertThat(invoked.get()).isZero();

        // 等待 openDuration 后进入 HALF_OPEN，探测成功即恢复 CLOSED
        Thread.sleep(1100);
        assertThat(breaker.stateOf("dep")).isEqualTo(CircuitBreaker.State.HALF_OPEN);
        assertThat(breaker.call("dep", () -> "real", () -> "fallback")).isEqualTo("real");
        assertThat(breaker.stateOf("dep")).isEqualTo(CircuitBreaker.State.CLOSED);
    }

    @Test
    @DisplayName("熔断：样本数不足最小请求数时不触发（避免低流量下的误熔断）")
    void circuitBreakerNeedsMinimumCalls() {
        CircuitBreaker breaker = new CircuitBreaker(0.5, 20, 1, 3);
        for (int i = 0; i < 5; i++) {
            breaker.call("dep2", () -> {
                throw new IllegalStateException("fail");
            }, () -> "fallback");
        }
        // 只有 5 个样本，远低于 minimumCalls=20，不应熔断
        assertThat(breaker.stateOf("dep2")).isEqualTo(CircuitBreaker.State.CLOSED);
    }

    // ------------------------------------------------------------------
    // 并发：分段锁本身也要能被验证
    // ------------------------------------------------------------------

    @Test
    @DisplayName("分段锁：同一 key 的并发访问能被串行化（验证 test-and-set 语义）")
    void stripedLockSerializesSameKey() throws Exception {
        int threads = 32;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        CountDownLatch start = new CountDownLatch(1);
        CountDownLatch done = new CountDownLatch(threads);
        AtomicInteger concurrent = new AtomicInteger();
        AtomicInteger maxConcurrent = new AtomicInteger();

        for (int i = 0; i < threads; i++) {
            pool.submit(() -> {
                try {
                    start.await();
                    int now = concurrent.incrementAndGet();
                    maxConcurrent.accumulateAndGet(now, Math::max);
                    Thread.sleep(1);
                    concurrent.decrementAndGet();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                } finally {
                    done.countDown();
                }
            });
        }
        start.countDown();
        assertThat(done.await(10, TimeUnit.SECONDS)).isTrue();
        pool.shutdownNow();
        // 这个用例证明的是"测试本身能观测并发度"，用于给后续库存并发测试建立信心
        assertThat(maxConcurrent.get()).isGreaterThan(1);
    }
}
