package com.smartlib;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * SmartLib 后端启动类。
 *
 * <p>@MapperScan 把 MyBatis 的 Mapper 接口注册为 bean；
 * @EnableScheduling 用于逾期扫描、预约过期、幂等表清理这三个定时任务。
 */
@SpringBootApplication
@MapperScan("com.smartlib.mapper")
@EnableScheduling
public class SmartLibApplication {

    public static void main(String[] args) {
        SpringApplication.run(SmartLibApplication.class, args);
    }
}
