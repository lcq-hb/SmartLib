package com.smartlib.service.search;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 轻量分词器。
 *
 * <p><b>为什么不用 IK / jieba 这类分词器</b>：它们都需要词典文件与额外依赖，
 * 而本项目的检索目标是"书名、作者、出版社、标签"这类短文本。
 * 对这种短文本，<b>二元切分（bigram）</b>的效果与词典分词非常接近，
 * 而且有两个关键优势：零词典维护、不会因为词典缺词而召回失败
 * （词典分词的典型故障是"新词搜不到"，这是图书馆场景最不能接受的）。
 *
 * <p><b>切分规则</b>：
 * <pre>
 *   "计算机网络"  → 计算、算机、机网、网络          （汉字 bigram）
 *   "Elasticsearch实战" → elasticsearch、el、la、…、实战  （英文整词 + bigram）
 *   "计算机网络"  → py:jsjwl、py:js、py:jsj …        （拼音首字母及其前缀）
 * </pre>
 *
 * <p><b>为什么 bigram 能支持"子串查询"</b>：如果文档里有 "elasticsearch"，
 * 它的 bigram 集合包含 el/la/as/st/ti/ic/cs/se/ea/ar/ch；
 * 查询 "elastic" 的 bigram 是 el/la/as/st/ti/ic，是前者的子集，因此必然命中。
 * 这个性质让 bigram 天然支持前缀与中缀匹配，无需额外的通配符查询。
 *
 * <p><b>为什么索引拼音前缀</b>：读者可能只输入 "jsj" 而没输全 "jsjwl"。
 * 索引全部前缀（长度 >= 2）后，任何前缀输入都能直接命中；
 * 代价是索引条目略微增加（一个 5 字书名多 3 条），完全可接受。
 */
public final class Tokenizer {

    /** 拼音 token 的统一前缀，避免与正常词项撞车。 */
    public static final String PINYIN_PREFIX = "py:";

    /** 需要被当作分隔符的字符（英文标点、空白、全角符号）。 */
    private static final String SEPARATORS = " \t\r\n,.;:!?\"'()[]{}<>|/\\-_+=*&^%$#@~`，。；：！？、（）【】《》“”‘’—…·";

    private Tokenizer() {
    }

    /**
     * 把一段文本切成词项集合（去重，保持顺序便于调试）。
     */
    public static List<String> tokenize(String text) {
        if (text == null || text.isBlank()) {
            return List.of();
        }
        Set<String> tokens = new LinkedHashSet<>();
        String lower = text.toLowerCase(Locale.ROOT);

        // 1. 按分隔符切开，得到若干"连续片段"
        for (String segment : split(lower)) {
            if (segment.isEmpty()) {
                continue;
            }
            addSegmentTokens(segment, tokens);
        }

        // 2. 整串的拼音首字母 + 全部前缀（长度 >= 2）
        //
        //    刻意不为"单字"生成 py:x 词项：汉字单字的首字母只有 23 种可能，
        //    一旦入库就会形成极长的倒排链（几乎每本书都命中），既污染相关度又浪费内存。
        //    单字查询本来就是低频需求，靠汉字本身的单字词项即可覆盖。
        if (PinyinUtils.hasChinese(text) && initialsLengthAtLeastTwo(text)) {
            String initials = PinyinUtils.initials(text);
            for (int len = 2; len <= initials.length(); len++) {
                tokens.add(PINYIN_PREFIX + initials.substring(0, len));
            }
        }

        // 3. 查询侧也要生成带前缀的拼音词项 —— 这是一处曾经真实存在的功能性缺陷：
        //
        //    索引侧把拼音词项存成 "py:jsjwl"（带前缀，避免与正常英文单词撞车），
        //    而查询侧一开始只生成了裸词项 "jsjwl"，两边永远匹配不上，
        //    结果就是"用首字母搜中文书名"这个功能完全不可用
        //    （实测：查询 jsj 返回 0 条，jsjwl 返回 0 条，中文关键词则正常）。
        //    单元测试之所以没发现，是因为它只验证了"索引侧生成了 py: 词项"，
        //    没有验证"查询词能与索引词对得上"—— 这类缺口只能靠端到端检索测试兜住
        //    （见 SearchEngineBenchmarkTest#pinyinInitialsSearchFindsChineseBook）。
        if (looksLikePinyinInitials(text)) {
            for (int len = 2; len <= text.length(); len++) {
                tokens.add(PINYIN_PREFIX + text.substring(0, len));
            }
        }
        return new ArrayList<>(tokens);
    }

    /**
     * 把一个连续片段切成词项。
     *
     * <p>汉字按 bigram 切；英文/数字整词保留，同时补 bigram 以支持子串匹配。
     * 片段长度为 1 时保留该单字（否则单字查询永远搜不到）。
     */
    private static void addSegmentTokens(String segment, Set<String> tokens) {
        int length = segment.length();
        if (length == 1) {
            tokens.add(segment);
            return;
        }
        if (isAscii(segment)) {
            // 英文/数字：整词 + bigram
            tokens.add(segment);
            for (int i = 0; i + 2 <= length; i++) {
                tokens.add(segment.substring(i, i + 2));
            }
            return;
        }
        // 含汉字：整段 + bigram。整段用于"精确短语"加权，bigram 用于模糊召回
        tokens.add(segment);
        for (int i = 0; i + 2 <= length; i++) {
            tokens.add(segment.substring(i, i + 2));
        }
        // 刻意不再为 2 字中文段落额外补单字词项：
        //   2 字段落的"整段"本身就是它的 bigram，单字索引对召回没有贡献，
        //   只会引入噪音 —— 实测搜"算法"会命中《新概念英语》，
        //   因为它的标签"语法"被拆出了单字"法"并与查询词"法"匹配上了。
        //   真正需要单字的情形（整段只有 1 个字）已在上面的 length == 1 分支处理。
    }

    private static List<String> split(String text) {
        List<String> segments = new ArrayList<>();
        StringBuilder current = new StringBuilder();
        for (int i = 0; i < text.length(); i++) {
            char ch = text.charAt(i);
            if (SEPARATORS.indexOf(ch) >= 0) {
                if (current.length() > 0) {
                    segments.add(current.toString());
                    current.setLength(0);
                }
            } else {
                current.append(ch);
            }
        }
        if (current.length() > 0) {
            segments.add(current.toString());
        }
        return segments;
    }

    private static boolean isAscii(String segment) {
        for (int i = 0; i < segment.length(); i++) {
            if (segment.charAt(i) > 127) {
                return false;
            }
        }
        return true;
    }

    private static boolean initialsLengthAtLeastTwo(String text) {
        return PinyinUtils.initials(text).length() >= 2;
    }

    /** 判断一个查询串是否"看起来像拼音首字母"（纯 ascii 字母、长度 >= 2），用于检索日志分析。 */
    public static boolean looksLikePinyinInitials(String query) {
        if (query == null || query.length() < 2) {
            return false;
        }
        for (int i = 0; i < query.length(); i++) {
            char ch = query.charAt(i);
            if (ch < 'a' || ch > 'z') {
                return false;
            }
        }
        return true;
    }
}
