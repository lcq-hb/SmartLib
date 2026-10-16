package com.smartlib.web;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HexFormat;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.annotation.Order;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;

import com.smartlib.common.Json;
import com.smartlib.common.Result;
import com.smartlib.common.ErrorCode;
import com.smartlib.common.TraceContext;

import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

/**
 * 内部接口签名校验（HMAC-SHA256）。
 *
 * <p><b>解决什么问题</b>：{@code /internal/agent/**} 是给 Agent 服务用的"后门"接口 ——
 * 它能直接替读者借书、查别人借阅。这些能力绝不能被公网直接访问，
 * 因此在网关层做了网段限制（只放行内网）之外，应用层再加一道<b>请求签名</b>：
 * 只有持有共享密钥的 Agent 服务才能构造出合法签名。
 *
 * <p><b>签名串格式</b>（Java 与 Python 两侧必须完全一致）：
 * <pre>
 *   canonical = timestamp + "\n" + METHOD + "\n" + pathWithQuery + "\n" + bodyUtf8
 *   sign      = lowercaseHex(HMAC_SHA256(secret, canonical))
 * </pre>
 *
 * <p><b>为什么把 timestamp 与 path 也纳入签名，而不只签 body</b>：
 * <ul>
 *   <li>签 timestamp + 校验时间窗口（±300s）→ 防止签名被截获后无限重放；</li>
 *   <li>签 method + path → 防止把一个"查库存"的签名挪用到"借书"接口上（签名越权）。</li>
 * </ul>
 *
 * <p><b>已知局限（诚实记录）</b>：这是请求级完整性校验，不是双向认证，
 * 也没有防篡改的时间同步机制；真正的内部通信应使用 mTLS。
 * 本项目选择 HMAC 是因为它在没有证书基础设施时部署成本最低，
 * 且足以挡住"内网被横向渗透后直接调用后门接口"这一类攻击。
 */
@Component
@Order(3)
public class InternalSignFilter implements Filter {

    private static final Logger log = LoggerFactory.getLogger(InternalSignFilter.class);

    public static final String HEADER_TIMESTAMP = "X-Internal-Timestamp";
    public static final String HEADER_SIGN = "X-Internal-Sign";

    private static final String ALGORITHM = "HmacSHA256";

    private final String secret;
    private final boolean enabled;
    private final long toleranceSeconds;

    public InternalSignFilter(
            @Value("${smartlib.internal.sign-secret:dev-internal-secret-please-change}") String secret,
            @Value("${smartlib.internal.sign-enabled:true}") boolean enabled,
            @Value("${smartlib.internal.timestamp-tolerance-seconds:300}") long toleranceSeconds) {
        this.secret = secret;
        this.enabled = enabled;
        this.toleranceSeconds = toleranceSeconds;
        if (enabled && "dev-internal-secret-please-change".equals(secret)) {
            log.warn("内部接口签名使用的是默认密钥，生产环境必须通过环境变量 SMARTLIB_INTERNAL_SIGN 覆盖！");
        }
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        if (!(request instanceof HttpServletRequest httpRequest)) {
            chain.doFilter(request, response);
            return;
        }
        String path = httpRequest.getRequestURI();
        if (!enabled || !path.startsWith("/internal/")) {
            chain.doFilter(request, response);
            return;
        }

        // 必须包装成可重复读取的请求：签名要读 body，控制器还要再读一次
        CachedBodyRequestWrapper wrapper = new CachedBodyRequestWrapper(httpRequest);
        String timestamp = wrapper.getHeader(HEADER_TIMESTAMP);
        String providedSign = wrapper.getHeader(HEADER_SIGN);

        String failure = verify(timestamp, providedSign, wrapper);
        if (failure != null) {
            log.warn("内部接口签名校验失败 path={} reason={} headers={}",
                    path, failure, wrapper.signingHeaders());
            writeUnauthorized((HttpServletResponse) response, failure);
            return;
        }
        chain.doFilter(wrapper, response);
    }

    /**
     * @return null 表示通过；否则返回失败原因
     */
    private String verify(String timestamp, String providedSign, CachedBodyRequestWrapper request) {
        if (timestamp == null || timestamp.isBlank()) {
            return "缺少 " + HEADER_TIMESTAMP;
        }
        if (providedSign == null || providedSign.isBlank()) {
            return "缺少 " + HEADER_SIGN;
        }
        long ts;
        try {
            ts = Long.parseLong(timestamp.trim());
        } catch (NumberFormatException e) {
            return HEADER_TIMESTAMP + " 不是合法整数";
        }
        long skew = Math.abs(System.currentTimeMillis() / 1000 - ts);
        if (skew > toleranceSeconds) {
            return "时间戳偏差 " + skew + " 秒，超出 ±" + toleranceSeconds + " 秒容忍窗口（可能被重放）";
        }

        String pathWithQuery = request.getRequestURI();
        if (request.getQueryString() != null) {
            pathWithQuery = pathWithQuery + "?" + request.getQueryString();
        }
        String canonical = timestamp + "\n"
                + request.getMethod().toUpperCase() + "\n"
                + pathWithQuery + "\n"
                + request.getBodyAsString();
        String expected = hmacHex(canonical);
        // 常量时间比较：避免通过响应时间差逐字节猜签名
        if (!MessageDigest.isEqual(expected.getBytes(StandardCharsets.UTF_8),
                providedSign.trim().toLowerCase().getBytes(StandardCharsets.UTF_8))) {
            return "签名不匹配";
        }
        return null;
    }

    private String hmacHex(String canonical) {
        try {
            Mac mac = Mac.getInstance(ALGORITHM);
            mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), ALGORITHM));
            return HexFormat.of().formatHex(mac.doFinal(canonical.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException("HMAC 计算失败", e);
        }
    }

    private void writeUnauthorized(HttpServletResponse response, String reason) throws IOException {
        response.setStatus(ErrorCode.INTERNAL_SIGN_INVALID.getHttpStatus());
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.setHeader(TraceContext.TRACE_ID_HEADER, TraceContext.get());
        response.getWriter().write(Json.toJson(
                Result.fail(ErrorCode.INTERNAL_SIGN_INVALID.getCode(), "签名校验失败：" + reason)));
        response.getWriter().flush();
    }
}
