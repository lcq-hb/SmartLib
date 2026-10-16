package com.smartlib.service;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.Duration;
import java.time.LocalDateTime;
import java.util.List;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import com.smartlib.domain.dto.Dtos.BorrowRule;
import com.smartlib.domain.entity.BorrowRecord;
import com.smartlib.mapper.BorrowRecordMapper;
import com.smartlib.mapper.ReaderMapper;
import com.smartlib.support.TokenBucketRateLimiter;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * 定时任务集合。
 *
 * <p><b>为什么用一个类集中管理</b>：定时任务是运维盲区（出问题时没人知道它跑没跑），
 * 集中在一起 + 统一日志前缀 + 统一指标，能让"任务是否健康"变得可观测。
 *
 * <p><b>幂等性要求</b>：每批处理都用条件更新/状态判断，重复执行不会产生副作用。
 * 例如逾期扫描只处理 {@code status='BORROWED'} 的记录，
 * 已经标成 OVERDUE 的下次不会被重复计费。
 *
 * <p><b>initialDelay 刻意设置得较大</b>：避免应用刚启动、连接池还没热起来时
 * 一批定时任务同时抢连接。这个细节在压测时影响很明显。
 */
@Component
@ConditionalOnProperty(name = "smartlib.scheduled.enabled", havingValue = "true", matchIfMissing = true)
public class ScheduledTasks {

    private static final Logger log = LoggerFactory.getLogger(ScheduledTasks.class);

    /** 每批最多处理的记录数，防止一次任务把数据库拖住。 */
    private static final int BATCH_SIZE = 500;
    /** 幂等记录保留天数。 */
    private static final int IDEMPOTENT_RETENTION_DAYS = 7;

    private final BorrowRecordMapper borrowMapper;
    private final ReaderMapper readerMapper;
    private final ReservationService reservationService;
    private final IdempotencyService idempotencyService;
    private final RuleEngine ruleEngine;
    private final TokenBucketRateLimiter rateLimiter;
    private final MeterRegistry meterRegistry;

    public ScheduledTasks(BorrowRecordMapper borrowMapper,
                          ReaderMapper readerMapper,
                          ReservationService reservationService,
                          IdempotencyService idempotencyService,
                          RuleEngine ruleEngine,
                          TokenBucketRateLimiter rateLimiter,
                          MeterRegistry meterRegistry) {
        this.borrowMapper = borrowMapper;
        this.readerMapper = readerMapper;
        this.reservationService = reservationService;
        this.idempotencyService = idempotencyService;
        this.ruleEngine = ruleEngine;
        this.rateLimiter = rateLimiter;
        this.meterRegistry = meterRegistry;
    }

    /**
     * 逾期扫描：把超期未还的记录标为 OVERDUE 并计算罚金。
     *
     * <p>为什么罚金在扫描时算而不是在查询时算：查询时算意味着每次列表接口都要
     * 遍历全部记录做日期运算；扫描时算一次落库，查询就是纯读，接口 P99 更稳定。
     * 代价是罚金最多滞后一个扫描周期（10 分钟），对 0.2 元/天的计费粒度完全够用。
     */
    @Scheduled(initialDelay = 60_000, fixedDelay = 600_000)
    @Transactional(rollbackFor = Exception.class)
    public void scanOverdue() {
        long start = System.currentTimeMillis();
        LocalDateTime now = LocalDateTime.now();
        List<BorrowRecord> dueRecords = borrowMapper.findDueBefore(now, BATCH_SIZE);
        int marked = 0;
        for (BorrowRecord record : dueRecords) {
            var reader = readerMapper.findById(record.getReaderId());
            BorrowRule rule = ruleEngine.ruleFor(reader == null ? "*" : reader.getType());
            long overdueDays = Math.max(1, Duration.between(record.getDueAt(), now).toDays());
            BigDecimal fine = rule.finePerDay().multiply(BigDecimal.valueOf(overdueDays))
                    .setScale(2, RoundingMode.HALF_UP);
            borrowMapper.updateStatusAndFine(record.getId(), "OVERDUE", fine);
            // 信用分只在"首次进入逾期"时扣一次：因为 updateStatusAndFine 把它移出了 BORROWED 集合，
            // 下次扫描不会再看到这条记录，天然避免了重复扣分
            readerMapper.addCreditScore(record.getReaderId(), -5);
            marked++;
        }
        if (marked > 0) {
            meterRegistry.counter("smartlib.job.overdue.marked").increment(marked);
            log.info("[定时任务] 逾期扫描完成：标记 {} 条，耗时 {}ms", marked, System.currentTimeMillis() - start);
        }
    }

    /**
     * 预约到馆超期处理：过期的预约出队并顺延给下一位。
     */
    @Scheduled(initialDelay = 90_000, fixedDelay = 300_000)
    public void expireReservations() {
        int handled = reservationService.expireReadyReservations(BATCH_SIZE);
        if (handled > 0) {
            meterRegistry.counter("smartlib.job.reservation.expired").increment(handled);
            log.info("[定时任务] 预约过期处理完成：{} 条", handled);
        }
    }

    /**
     * 清理过期幂等记录。每天凌晨 3 点执行，避开借阅高峰。
     *
     * <p><b>这里不能加 {@code initialDelay}</b>：Spring 对 cron 触发器明确不支持该属性，
     * 会在创建 bean 时直接抛
     * {@code IllegalStateException: 'initialDelay' not supported for cron triggers}，
     * 导致应用启动失败。而且对这个任务来说 initialDelay 本来也没有意义 ——
     * cron 表达式已经保证它只会在凌晨 3 点触发。
     *
     * <p>这个坑在单元测试里<b>看不见</b>（测试 profile 用
     * {@code smartlib.scheduled.enabled=false} 关掉了整个 bean），
     * 只有真正把应用启动起来才会暴露。因此后续新增了
     * {@code ScheduledTasksWiringTest} 专门守护定时任务的装配。
     */
    @Scheduled(cron = "0 0 3 * * ?")
    public void cleanupIdempotentRecords() {
        int deleted = idempotencyService.cleanup(LocalDateTime.now().minusDays(IDEMPOTENT_RETENTION_DAYS));
        if (deleted > 0) {
            meterRegistry.counter("smartlib.job.idempotent.cleaned").increment(deleted);
        }
    }

    /**
     * 清理空闲的限流令牌桶，防止限流维度（如按 IP）无限增长导致内存泄漏。
     */
    @Scheduled(initialDelay = 150_000, fixedDelay = 600_000)
    public void evictIdleRateLimitBuckets() {
        int before = rateLimiter.trackedKeys();
        rateLimiter.evictIdle();
        int after = rateLimiter.trackedKeys();
        if (before != after) {
            log.info("[定时任务] 限流桶清理：{} -> {}", before, after);
        }
    }
}
