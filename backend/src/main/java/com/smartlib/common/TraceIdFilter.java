package com.smartlib.common;

import java.io.IOException;
import java.util.UUID;

import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;

import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

/**
 * traceId 入口过滤器。
 *
 * <p>为什么用 {@code @Component + @Order} 而不是 FilterRegistrationBean：
 * 更少的框架耦合，跨 Spring Boot 版本更稳定（Boot 4 调整过若干 web 相关 API）。
 */
@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class TraceIdFilter implements Filter {

    private static final int TRACE_ID_LENGTH = 16;

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        String traceId = null;
        if (request instanceof HttpServletRequest httpRequest) {
            traceId = httpRequest.getHeader(TraceContext.TRACE_ID_HEADER);
        }
        if (traceId == null || traceId.isBlank()) {
            traceId = UUID.randomUUID().toString().replace("-", "").substring(0, TRACE_ID_LENGTH);
        }

        TraceContext.set(traceId);
        if (response instanceof HttpServletResponse httpResponse) {
            httpResponse.setHeader(TraceContext.TRACE_ID_HEADER, traceId);
        }
        try {
            chain.doFilter(request, response);
        } finally {
            // 必须清理：Tomcat 线程池会复用线程，不清理会导致 traceId 串号
            TraceContext.clear();
        }
    }
}
