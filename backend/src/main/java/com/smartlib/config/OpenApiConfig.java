package com.smartlib.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Contact;
import io.swagger.v3.oas.models.info.Info;
import io.swagger.v3.oas.models.info.License;

/**
 * OpenAPI 文档配置。
 *
 * <p>启动后访问 {@code /swagger-ui.html}，可以看到三组接口：
 * 公共 API、Agent 内部工具接口、管理接口。
 * 把接口文档作为交付物的一部分，是为了让"前后端/Agent 联调"不必靠口头约定 ——
 * 这也是接口契约能被冻结的前提。
 */
@Configuration
public class OpenApiConfig {

    @Bean
    public OpenAPI smartLibOpenApi() {
        return new OpenAPI()
                .info(new Info()
                        .title("SmartLib 高校图书馆智能借阅与推荐平台 API")
                        .version("1.0.0")
                        .description("""
                                Java 后端对外提供两类接口：
                                1) 公共 API（/api/**）：读者与馆员使用，统一响应体 {code,msg,data,traceId}；
                                2) Agent 工具接口（/internal/agent/**）：供 Python Agent 服务调用，
                                   需要 X-Internal-Timestamp + X-Internal-Sign（HMAC-SHA256）签名。

                                关键约定：
                                - 写操作（借书/续借/预约）必须带幂等键；
                                - 库存查询返回 availableCopies = -1 表示"暂时无法确认"（降级态），不是 0；
                                - 所有响应都带 traceId，与 Agent 服务共用同一个 trace，便于跨进程排查。
                                """)
                        .contact(new Contact().name("SmartLib 项目组"))
                        .license(new License().name("MIT")));
    }
}
