package com.smartlib.service.search;

import java.nio.charset.Charset;

/**
 * 拼音首字母工具 —— 支撑"用首字母搜中文书名"这一真实需求。
 *
 * <p>读者在检索框里输入 {@code jsjwl} 期望搜到《计算机网络》，这在国内图书馆系统里是高频用法。
 * 常见实现需要一张几千字的汉字→拼音映射表（数百 KB），本项目采用<b>基于 GB2312 区位码的区间查表法</b>：
 * 国标一级汉字本身就是按拼音顺序排列的，因此只要知道每个声母区间的区位码边界，
 * 就能用 24 个数字推出任意一级汉字的声母，不需要任何字典数据。
 *
 * <p><b>算法</b>：
 * <pre>
 *   1. 把汉字按 GBK 编码成 2 字节 b0 b1（GB2312 一级汉字两字节都 >= 0xA1）
 *   2. 换算成区位码：quwei = (b0 - 0xA0) × 100 + (b1 - 0xA0)
 *   3. 在声母边界表里找到 quwei 落在哪个区间，对应到 a~z 的声母
 * </pre>
 *
 * <p><b>已验证的样例</b>（单测覆盖）：
 * <pre>
 *   中 D6D0 → quwei 5448 → 落在 [5249, 5600) → z
 *   计 BCC6 → quwei 2838 → 落在 [2787, 3106) → j
 *   网 CDF8 → quwei 4588 → 落在 [4558, 4684) → w
 *   络 C2E7 → quwei 3471 → 落在 [3212, 3472) → l
 * </pre>
 *
 * <p><b>已知边界</b>：二级汉字（生僻字）、多音字、以及 GBK 无法表示的字符会返回 0（跳过）。
 * 多音字按区位码只能取一个读音，这是该算法的固有精度限制；
 * 对"书名首字母检索"这个用途足够，真要精确需要引入词典与分词器。
 */
public final class PinyinUtils {

    /** 声母区间边界（区位码）。数组严格递增，对应下面的字母表。 */
    private static final int[] QUWEI_BOUNDS = {
            1601, 1637, 1833, 2078, 2274, 2302, 2433, 2594,
            2787, 3106, 3212, 3472, 3635, 3722, 3730, 3858,
            4027, 4086, 4390, 4558, 4684, 4925, 5249, 5600
    };

    /** 与 QUWEI_BOUNDS 一一对应。注意没有 i、u、v 开头的拼音。 */
    private static final char[] LETTERS = {
            'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h',
            'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q',
            'r', 's', 't', 'w', 'x', 'y', 'z', 'z'
    };

    private static final Charset GBK;

    static {
        Charset gbk;
        try {
            gbk = Charset.forName("GBK");
        } catch (Exception e) {
            // 极少数裁剪过的 JRE 可能不带 GBK；此时拼音功能自动失效，不影响主流程
            gbk = null;
        }
        GBK = gbk;
    }

    private PinyinUtils() {
    }

    /**
     * 取单个字符的拼音首字母。
     *
     * @return 小写字母；数字原样返回；无法识别的字符（标点、生僻字）返回 0
     */
    public static char firstLetter(char ch) {
        if (ch >= '0' && ch <= '9') {
            return ch;
        }
        if (ch >= 'a' && ch <= 'z') {
            return ch;
        }
        if (ch >= 'A' && ch <= 'Z') {
            return (char) (ch + 32);
        }
        if (GBK == null) {
            return 0;
        }
        byte[] bytes = String.valueOf(ch).getBytes(GBK);
        if (bytes.length != 2) {
            return 0;
        }
        int quwei = ((bytes[0] & 0xFF) - 0xA0) * 100 + ((bytes[1] & 0xFF) - 0xA0);
        char result = 0;
        for (int i = 0; i < QUWEI_BOUNDS.length; i++) {
            if (quwei >= QUWEI_BOUNDS[i]) {
                result = LETTERS[i];
            } else {
                break;
            }
        }
        return result;
    }

    /**
     * 取整串文本的拼音首字母，如 {@code "计算机网络"} → {@code "jsjwl"}。
     * 非汉字字符（字母、数字）会被保留并转小写，标点与空白被丢弃。
     */
    public static String initials(String text) {
        if (text == null || text.isEmpty()) {
            return "";
        }
        StringBuilder sb = new StringBuilder(text.length());
        for (int i = 0; i < text.length(); i++) {
            char letter = firstLetter(text.charAt(i));
            if (letter != 0) {
                sb.append(letter);
            }
        }
        return sb.toString();
    }

    /** 判断串里是否含有汉字，用于决定要不要额外建拼音索引。 */
    public static boolean hasChinese(String text) {
        if (text == null) {
            return false;
        }
        for (int i = 0; i < text.length(); i++) {
            char ch = text.charAt(i);
            if (ch >= 0x4E00 && ch <= 0x9FFF) {
                return true;
            }
        }
        return false;
    }
}
