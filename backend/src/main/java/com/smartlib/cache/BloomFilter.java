package com.smartlib.cache;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.BitSet;

/**
 * 布隆过滤器 —— 防缓存穿透。
 *
 * <p><b>解决什么问题：</b>如果有人用不存在的 bookId 反复请求（无论是恶意爬虫还是坏掉的前端），
 * 每次都会绕过缓存直接查数据库（缓存里没有这个 key，自然也命中不了），
 * 数据库就成了被反复打的全表扫描对象。布隆过滤器能在<strong>内存里</strong>
 * 用几个 bit 判断"这个 id 一定不存在"，把这类请求挡在数据库之前。
 *
 * <p><b>必须讲清楚的语义（面试重点）</b>：
 * <ul>
 *   <li>说"不存在"→ <b>一定不存在</b>（无假阴性），因此可以安全地直接拒绝；</li>
 *   <li>说"存在"→ <b>可能不存在</b>（有假阳性），因此还要继续走正常查询流程；</li>
 *   <li>因此布隆过滤器只能减少无效查询，<b>不能替代缓存空值</b>，
 *       两者是"概率拦截 + 精确兜底"的组合。</li>
 * </ul>
 *
 * <p><b>参数估算（本项目实际取值）</b>：按 n = 120 万书目、可接受误判率 p = 0.1%
 * <pre>
 *   位数组长度 m = -n·ln(p) / (ln2)²  = -1.2e6 × (-6.9078) / 0.4805 ≈ 1.73e7 bit ≈ 2.06 MB
 *   哈希函数个数 k = (m/n)·ln2        ≈ 17.25e6 / 1.2e6 × 0.693 ≈ 10
 * </pre>
 * 即 2 MB 内存换来 99.9% 的无效请求拦截率，性价比极高。
 *
 * <p><b>不支持删除</b>：这是布隆过滤器的固有限制（把某个 bit 置 0 会影响其他元素），
 * 所以本项目对"删书"采取的策略是"逻辑删除 + 保留在过滤器里"，宁可多放行几次查询，
 * 也不能因为误删而把真实存在的书判为不存在（假阴性会导致功能错误，不可接受）。
 */
public class BloomFilter {

    private final BitSet bits;
    private final int bitSize;
    private final int hashCount;
    private long insertedCount;

    public BloomFilter(long expectedInsertions, double falsePositiveRate) {
        if (expectedInsertions <= 0) {
            throw new IllegalArgumentException("expectedInsertions 必须为正数");
        }
        if (falsePositiveRate <= 0 || falsePositiveRate >= 1) {
            throw new IllegalArgumentException("falsePositiveRate 必须在 (0,1) 之间");
        }
        this.bitSize = optimalBitSize(expectedInsertions, falsePositiveRate);
        this.hashCount = optimalHashCount(expectedInsertions, bitSize);
        this.bits = new BitSet(bitSize);
    }

    /** m = -n·ln(p) / (ln2)² */
    public static int optimalBitSize(long n, double p) {
        double ln2 = Math.log(2);
        return (int) Math.ceil(-n * Math.log(p) / (ln2 * ln2));
    }

    /** k = (m/n)·ln2，至少 1 个。 */
    public static int optimalHashCount(long n, int m) {
        return Math.max(1, (int) Math.round((double) m / n * Math.log(2)));
    }

    public void add(String value) {
        long[] h = hashes(value);
        for (int i = 0; i < hashCount; i++) {
            bits.set(index(h[0], h[1], i));
        }
        insertedCount++;
    }

    /**
     * @return false 表示<b>一定不存在</b>；true 表示可能存在
     */
    public boolean mightContain(String value) {
        long[] h = hashes(value);
        for (int i = 0; i < hashCount; i++) {
            if (!bits.get(index(h[0], h[1], i))) {
                return false;
            }
        }
        return true;
    }

    /**
     * 用 Kirsch-Mitzenmacher 方法从两个独立哈希推导出 k 个哈希：
     * {@code h_i = h1 + i·h2}。这样只需要计算一次摘要，避免了 k 次哈希计算的开销。
     */
    private int index(long h1, long h2, int i) {
        long combined = h1 + (long) i * h2;
        return (int) Math.floorMod(combined, bitSize);
    }

    private long[] hashes(String value) {
        try {
            MessageDigest digest = MessageDigest.getInstance("MD5");
            byte[] bytes = digest.digest(value.getBytes(StandardCharsets.UTF_8));
            // 取摘要的前 16 字节拆成两个 long 作为 h1/h2
            long h1 = 0;
            long h2 = 0;
            for (int i = 0; i < 8; i++) {
                h1 = (h1 << 8) | (bytes[i] & 0xFF);
                h2 = (h2 << 8) | (bytes[i + 8] & 0xFF);
            }
            return new long[]{h1, h2};
        } catch (Exception e) {
            throw new IllegalStateException("MD5 不可用，布隆过滤器无法工作", e);
        }
    }

    /** 已插入元素个数。 */
    public long insertedCount() {
        return insertedCount;
    }

    public int bitSize() {
        return bitSize;
    }

    public int hashCount() {
        return hashCount;
    }

    /** 占用内存（字节），用于文档里的资源测算。 */
    public long memoryBytes() {
        return (long) Math.ceil(bitSize / 8.0);
    }

    /** 当前置位比例达到 100% 时过滤器就失效了，监控它可提前发现"容量估小了"。 */
    public double saturation() {
        return (double) bits.cardinality() / bitSize;
    }

    /** 便于在日志/文档中展示的参数摘要。 */
    public String describe() {
        return String.format("布隆过滤器[m=%d bit, k=%d, 内存=%.2f MB, 已插入=%d, 置位率=%.4f]",
                bitSize, hashCount, memoryBytes() / 1024.0 / 1024.0, insertedCount, saturation());
    }

    /** 供文档与单测展示"理论误判率"，用当前插入量代入公式。 */
    public double theoreticalFalsePositiveRate() {
        if (insertedCount == 0) {
            return 0.0;
        }
        double exponent = -((double) hashCount * insertedCount) / bitSize;
        return Math.pow(1 - Math.exp(exponent), hashCount);
    }
}
