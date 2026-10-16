package com.smartlib.web;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.annotation.Order;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;

import com.smartlib.common.ErrorCode;
import com.smartlib.common.Json;
import com.smartlib.common.Result;
import com.smartlib.common.TraceContext;
import com.smartlib.support.TokenBucketRateLimiter;

import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

/**
 * 入口限流过滤器。
 *
 * <p><b>为什么在 Filter 层限流而不是在 Controller 里</b>：
 * 要在请求进入业务逻辑（占线程、占数据库连接、占缓存）之前就把超额流量挡掉。
 * 到了 Controller 才拒绝，资源已经消耗了。
 *
 * <p><b>限流维度</b>：
 * <ul>
 *   <li>{@code /api/**} 按客户端 IP 限流 —— 防单个客户端刷接口；</li>
 *   <li>{@code /internal/**} 按"内部调用方"整体限流 —— 保护后端不被 Agent 的异常重试打爆；</li>
 *   <li>{@code /actuator/**} 不限流 —— 监控探针必须始终可达，
 *       否则限流会把健康检查也一起拒掉，K8s 会误判实例已死并触发重启雪崩。</li>
 * </ul>
 *
 * <p><b>被限流时应答</b>：429 + {@code Retry-After} 头。
 * 明确告诉调用方"过一会儿再来"，而不是让它立即重试（那会加剧拥塞）。
 */
@Component
@Order(2)
public class RateLimitFilter implements Filter {

    private static final Logger log = LoggerFactory.getLogger(RateLimitFilter.class);

    /** 建议客户端在多少秒后重试。 */
    private static final String RETRY_AFTER_SECONDS = "1";

    private final TokenBucketRateLimiter rateLimiter;
    private final boolean trustProxyHeader;

    public RateLimitFilter(TokenBucketRateLimiter rateLimiter,
                           @Value("${smartlib.rate-limit.trust-proxy-header:false}") boolean trustProxyHeader) {
        this.rateLimiter = rateLimiter;
        this.trustProxyHeader = trustProxyHeader;
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        if (!(request instanceof HttpServletRequest httpRequest)) {
            chain.doFilter(request, response);
            return;
        }
        String path = httpRequest.getRequestURI();
        if (path.startsWith("/actuator") || !isLimited(path)) {
            chain.doFilter(request, response);
            return;
        }

        String key = buildKey(httpRequest, path);
        if (!rateLimiter.tryAcquire(key)) {
            log.warn("限流触发 key={} path={}", key, path);
            writeTooManyRequests((HttpServletResponse) response);
            return;
        }
        chain.doFilter(request, response);
    }

    private boolean isLimited(String path) {
        return path.startsWith("/api/") || path.startsWith("/internal/");
    }

    private String buildKey(HttpServletRequest request, String path) {
        if (path.startsWith("/internal/")) {
            // 内部接口整体限流：Agent 的异常重试不应该把后端打爆
            return "internal:all";
        }
        return "api:" + clientIp(request);
    }

    /**
     * 取客户端 IP。
     *
     * <p>默认<b>不信</b> {@code X-Forwarded-For}：该头可以被客户端伪造，
     * 一旦信任，攻击者只要每次换一个假 IP 就能绕过限流。
     * 只有在确认前置了可信代理（Nginx 会覆盖该头）时才通过配置打开。
     */
    private String clientIp(HttpServletRequest request) {
        if (trustProxyHeader) {
            String forwarded = request.getHeader("X-Forwarded-For");
            if (forwarded != null && !forwarded.isBlank()) {
                int comma = forwarded.indexOf(',');
                return comma > 0 ? forwarded.substring(0, comma).trim() : forwarded.trim();
            }
        }
        return request.getRemoteAddr();
    }

    private void writeTooManyRequests(HttpServletResponse response) throws IOException {
        response.setStatus(429);
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.setHeader("Retry-After", RETRY_AFTER_SECONDS);
        response.setHeader(TraceContext.TRACE_ID_HEADER, TraceContext.get());
        response.getWriter().write(Json.toJson(Result.fail(ErrorCode.RATE_LIMITED)));
        response.getWriter().flush();
    }
}
