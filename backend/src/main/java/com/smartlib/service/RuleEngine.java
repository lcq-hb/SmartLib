package com.smartlib.service;

import java.math.BigDecimal;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import com.smartlib.common.BizException;
import com.smartlib.common.ErrorCode;
import com.smartlib.domain.dto.Dtos.BorrowRule;
import com.smartlib.domain.entity.Reader;
import com.smartlib.domain.entity.RuleConfig;
import com.smartlib.mapper.RuleConfigMapper;

import jakarta.annotation.PostConstruct;

/**
 * 借阅规则引擎 —— 让"改规则不发版"成立。
 *
 * <p><b>解决什么问题</b>：借阅册数、借期、续借次数原本散落在存储过程和 Java 常量里，
 * 每次调整（比如考试周临时把本科生借期从 30 天改成 45 天）都要走一次发版流程，
 * 平均 3 天才能生效。现在这些值都在 {@code rule_config} 表里，改完刷新缓存即可生效。
 *
 * <p><b>取值优先级</b>：读者类型精确匹配 &gt; {@code '*'} 通配。
 * 例如 {@code max_borrow_count|POSTGRAD=15} 与 {@code max_borrow_count|*=10} 同时存在时，
 * 研究生取 15。
 *
 * <p><b>缓存策略</b>：规则是读多写极少的数据（可能几个月改一次），
 * 因此全量加载进内存，只在管理接口显式刷新时重载。
 * 不做 TTL 自动过期是刻意的 —— 规则变更必须可控，不能出现"两个实例的规则不一致"的窗口期。
 *
 * <p><b>参数缺失时的行为</b>：取内置默认值并打 WARN，而不是抛异常。
 * 理由：少配一条规则不应该让所有人无法借书。
 */
@Component
public class RuleEngine {

    private static final Logger log = LoggerFactory.getLogger(RuleEngine.class);

    /** 内置兜底默认值，仅在表里查不到对应规则时使用。 */
    private static final int DEFAULT_MAX_BORROW = 10;
    private static final int DEFAULT_LOAN_DAYS = 30;
    private static final int DEFAULT_MAX_RENEW = 2;
    private static final int DEFAULT_RENEW_DAYS = 30;
    private static final BigDecimal DEFAULT_FINE_PER_DAY = new BigDecimal("0.20");
    private static final BigDecimal DEFAULT_FINE_FREE_LIMIT = new BigDecimal("10.00");
    private static final int DEFAULT_MIN_CREDIT = 60;
    private static final int DEFAULT_RESERVATION_HOLD_DAYS = 3;
    private static final int DEFAULT_MAX_RENEW_DAYS = 180;

    private final RuleConfigMapper ruleConfigMapper;

    /** key = ruleKey + '|' + readerType（含 '*' 兜底行），value = 原始字符串。 */
    private volatile Map<String, String> rules = Map.of();

    public RuleEngine(RuleConfigMapper ruleConfigMapper) {
        this.ruleConfigMapper = ruleConfigMapper;
    }

    @PostConstruct
    public void load() {
        refresh();
    }

    /** 重新加载规则。管理接口修改规则后必须调用，否则只有重启才生效。 */
    public synchronized int refresh() {
        List<RuleConfig> all = ruleConfigMapper.findAll();
        Map<String, String> loaded = new HashMap<>(all.size() * 2);
        for (RuleConfig config : all) {
            // 注意：这里不能往 map 里塞 null 值 —— Map.copyOf 遇到 null value 会抛 NPE。
            // 通配 '*' 的兜底逻辑由 raw() 在查询时处理：查不到 key|类型 就去查 key|*
            loaded.put(config.getRuleKey() + "|" + config.getReaderType(), config.getRuleValue());
        }
        this.rules = Map.copyOf(loaded);
        log.info("借阅规则已加载，共 {} 条", all.size());
        return all.size();
    }

    /**
     * 解析某个读者类型生效的完整规则。
     */
    public BorrowRule ruleFor(String readerType) {
        String type = readerType == null ? "*" : readerType;
        return new BorrowRule(
                intOf("max_borrow_count", type, DEFAULT_MAX_BORROW),
                intOf("loan_days", type, DEFAULT_LOAN_DAYS),
                intOf("max_renew_count", type, DEFAULT_MAX_RENEW),
                intOf("renew_days", type, DEFAULT_RENEW_DAYS),
                decimalOf("fine_per_day", type, DEFAULT_FINE_PER_DAY),
                decimalOf("fine_free_limit", type, DEFAULT_FINE_FREE_LIMIT),
                intOf("min_credit_score", type, DEFAULT_MIN_CREDIT),
                intOf("reservation_hold_days", type, DEFAULT_RESERVATION_HOLD_DAYS),
                intOf("max_renew_days", type, DEFAULT_MAX_RENEW_DAYS));
    }

    /**
     * 借阅前置校验。
     *
     * <p>把校验集中在规则引擎里，而不是散落在 Service 的 if 里，
     * 好处是"规则"这件事有唯一的落点，新增规则只改这里 + 加一条配置。
     *
     * @param reader          读者
     * @param activeCount     当前在借册数（含逾期未还）
     * @param hasOverdue      是否存在逾期
     * @param sameBookActive  是否已借同一本书
     * @param unpaidFine      未缴罚金
     */
    public void validateBorrow(Reader reader, int activeCount, boolean hasOverdue,
                               boolean sameBookActive, BigDecimal unpaidFine) {
        BorrowRule rule = ruleFor(reader.getType());

        if (!"ACTIVE".equals(reader.getStatus())) {
            throw new BizException(ErrorCode.READER_FROZEN);
        }
        if (reader.getCreditScore() != null && reader.getCreditScore() < rule.minCreditScore()) {
            throw new BizException(ErrorCode.CREDIT_TOO_LOW,
                    String.format("信用分 %d 低于可借阈值 %d", reader.getCreditScore(), rule.minCreditScore()));
        }
        if (hasOverdue) {
            throw new BizException(ErrorCode.READER_HAS_OVERDUE);
        }
        if (unpaidFine != null && unpaidFine.compareTo(rule.fineFreeLimit()) > 0) {
            throw new BizException(ErrorCode.FINE_UNPAID,
                    String.format("未缴罚金 %s 元已超过 %s 元上限", unpaidFine, rule.fineFreeLimit()));
        }
        if (sameBookActive) {
            throw new BizException(ErrorCode.BOOK_ALREADY_BORROWED);
        }
        if (activeCount + 1 > rule.maxBorrowCount()) {
            throw new BizException(ErrorCode.BORROW_LIMIT_EXCEEDED,
                    String.format("当前在借 %d 册，上限 %d 册", activeCount, rule.maxBorrowCount()));
        }
    }

    /**
     * 续借校验。
     *
     * @param renewCount    已续借次数
     * @param hasReservation 该书是否有等待中的预约
     */
    public void validateRenew(Reader reader, int renewCount, boolean hasReservation) {
        BorrowRule rule = ruleFor(reader.getType());
        if (hasReservation) {
            // 有人排队时不允许续借，否则队列会被无限延长 —— 这是"公平性"优先于"便利性"的规则
            throw new BizException(ErrorCode.RENEW_HAS_RESERVATION);
        }
        if (renewCount + 1 > rule.maxRenewCount()) {
            throw new BizException(ErrorCode.RENEW_LIMIT_EXCEEDED,
                    String.format("已续借 %d 次，上限 %d 次", renewCount, rule.maxRenewCount()));
        }
    }

    private String raw(String ruleKey, String readerType) {
        Map<String, String> snapshot = this.rules;
        String exact = snapshot.get(ruleKey + "|" + readerType);
        if (exact != null) {
            return exact;
        }
        return snapshot.get(ruleKey + "|*");
    }

    private int intOf(String ruleKey, String readerType, int defaultValue) {
        String value = raw(ruleKey, readerType);
        if (value == null || value.isBlank()) {
            log.warn("规则 [{}|{}] 未配置，使用默认值 {}", ruleKey, readerType, defaultValue);
            return defaultValue;
        }
        try {
            return Integer.parseInt(value.trim());
        } catch (NumberFormatException e) {
            log.warn("规则 [{}|{}] 的值 [{}] 不是合法整数，使用默认值 {}", ruleKey, readerType, value, defaultValue);
            return defaultValue;
        }
    }

    private BigDecimal decimalOf(String ruleKey, String readerType, BigDecimal defaultValue) {
        String value = raw(ruleKey, readerType);
        if (value == null || value.isBlank()) {
            log.warn("规则 [{}|{}] 未配置，使用默认值 {}", ruleKey, readerType, defaultValue);
            return defaultValue;
        }
        try {
            return new BigDecimal(value.trim());
        } catch (NumberFormatException e) {
            log.warn("规则 [{}|{}] 的值 [{}] 不是合法数字，使用默认值 {}", ruleKey, readerType, value, defaultValue);
            return defaultValue;
        }
    }

    /** 当前规则快照，供管理接口展示。 */
    public Map<String, String> snapshot() {
        return rules;
    }
}
