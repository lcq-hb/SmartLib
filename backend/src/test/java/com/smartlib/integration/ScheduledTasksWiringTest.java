package com.smartlib.integration;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;

import com.smartlib.service.ScheduledTasks;

/**
 * 定时任务装配回归测试。
 *
 * <p><b>这个类是为了守护一个"测试测不到、只有真机启动才炸"的缺陷而存在的。</b>
 *
 * <p>背景：{@code ScheduledTasks.cleanupIdempotentRecords()} 原本写成
 * {@code @Scheduled(initialDelay = 120_000, cron = "0 0 3 * * ?")}。
 * Spring 对 cron 触发器不支持 {@code initialDelay}，会在创建 bean 时抛
 * {@code IllegalStateException: 'initialDelay' not supported for cron triggers}，
 * <b>应用直接启动失败</b>。
 *
 * <p>而它躲过了整套测试：因为测试 profile 用
 * {@code smartlib.scheduled.enabled=false} 关掉了这个 bean（这是对的 ——
 * 测试期间不该跑定时任务），代价是"定时任务的装配正确性"失去了覆盖。
 *
 * <p>解决办法就是这个类：<b>用一个显式打开该开关的上下文，专门验证定时任务能被正确装配</b>。
 * 它不关心任务逻辑（那由各自的业务测试覆盖），只保证"应用能带着定时任务启动起来"。
 */
@SpringBootTest(properties = {
        "smartlib.scheduled.enabled=true",
        // 探针专用端口，避免与其它测试类的上下文冲突，也避免真的占用 8080
        "server.port=0"
})
@ActiveProfiles("test")
class ScheduledTasksWiringTest {

    @Autowired
    private ScheduledTasks scheduledTasks;

    @Test
    @DisplayName("定时任务装配：开关打开时 bean 能创建成功（cron 与 initialDelay 配置合法）")
    void scheduledTasksCanBeWired() {
        assertThat(scheduledTasks)
                .as("定时任务 bean 必须能在 smartlib.scheduled.enabled=true 下成功装配，"
                        + "否则应用无法启动")
                .isNotNull();
    }
}
