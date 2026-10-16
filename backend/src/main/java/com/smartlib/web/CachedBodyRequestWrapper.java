package com.smartlib.web;

import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.Enumeration;
import java.util.HashMap;
import java.util.Map;

import jakarta.servlet.ReadListener;
import jakarta.servlet.ServletInputStream;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;

/**
 * 可重复读取 body 的请求包装器。
 *
 * <p><b>为什么需要它</b>：内部接口要用 HMAC 校验请求体完整性，
 * 而校验发生在 Filter 里 —— 此时必须先读完 body 才能算签名。
 * Servlet 的 {@code getInputStream()} 是一次性的，读完之后
 * {@code @RequestBody} 就拿不到内容了（表现为控制器收到空 body）。
 *
 * <p>Spring 自带的 {@code ContentCachingRequestWrapper} 解决不了这个问题：
 * 它的 {@code getInputStream()} 仍然返回同一条底层流，
 * 在 Filter 里读完就没了。因此这里自己实现：
 * <b>构造时一次性读完并缓存字节，之后每次 getInputStream 都返回缓存的新流</b>。
 *
 * <p>代价是请求体必须完整驻留内存。因此本包装器只作用于 {@code /internal/**}
 * 这类请求体很小（KB 级）的内部接口，公共 API 不经过它，避免大 body 造成内存放大。
 */
public class CachedBodyRequestWrapper extends HttpServletRequestWrapper {

    private final byte[] cachedBody;

    public CachedBodyRequestWrapper(HttpServletRequest request) throws IOException {
        super(request);
        this.cachedBody = request.getInputStream().readAllBytes();
    }

    public byte[] getCachedBody() {
        return cachedBody;
    }

    public String getBodyAsString() {
        return new String(cachedBody, StandardCharsets.UTF_8);
    }

    @Override
    public ServletInputStream getInputStream() {
        ByteArrayInputStream buffer = new ByteArrayInputStream(cachedBody);
        return new ServletInputStream() {
            @Override
            public boolean isFinished() {
                return buffer.available() == 0;
            }

            @Override
            public boolean isReady() {
                return true;
            }

            @Override
            public void setReadListener(ReadListener readListener) {
                // 同步 Servlet 模型下不需要实现
            }

            @Override
            public int read() {
                return buffer.read();
            }
        };
    }

    @Override
    public BufferedReader getReader() {
        return new BufferedReader(new InputStreamReader(getInputStream(), StandardCharsets.UTF_8));
    }

    /** Content-Length 保持与原请求一致，便于下游做长度校验。 */
    @Override
    public int getContentLength() {
        return cachedBody.length;
    }

    @Override
    public long getContentLengthLong() {
        return cachedBody.length;
    }

    /** 便于排查"签名算出来不一致"的问题：把参与签名的头原样打印出来。 */
    public Map<String, String> signingHeaders() {
        Map<String, String> headers = new HashMap<>();
        Enumeration<String> names = getHeaderNames();
        if (names != null) {
            for (String name : Collections.list(names)) {
                if (name.toLowerCase().startsWith("x-internal")) {
                    headers.put(name, getHeader(name));
                }
            }
        }
        return headers;
    }
}
