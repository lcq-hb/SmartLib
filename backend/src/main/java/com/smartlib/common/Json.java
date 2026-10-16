package com.smartlib.common;

import tools.jackson.databind.DeserializationFeature;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.json.JsonMapper;

/**
 * 独立于 Spring 容器的 JSON 工具。
 *
 * <p>用途：幂等记录的结果序列化/反序列化等发生在非 Web 上下文的场景。
 * 直接依赖 Spring 注入的 ObjectMapper 会让这些工具类隐式依赖容器，
 * 因此这里显式构造一个私有实例。
 *
 * <p><b>Jackson 3 注意点</b>：Spring Boot 4 起默认 JSON 实现升级为 Jackson 3，
 * 包名由 {@code com.fasterxml.jackson} 变为 {@code tools.jackson}，
 * 且 Java 8 时间类型（LocalDateTime 等）的支持已内置到 databind
 * （{@code tools.jackson.databind.ext.javatime}），不再需要单独注册 JavaTimeModule。
 * 仍使用 Jackson 2 包名会导致编译期找不到类，这是从 Boot 3 升级最常见的问题之一。
 */
public final class Json {

    private static final ObjectMapper MAPPER = JsonMapper.builder()
            .disable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
            .build();

    private Json() {
    }

    public static ObjectMapper mapper() {
        return MAPPER;
    }

    public static String toJson(Object value) {
        if (value == null) {
            return null;
        }
        try {
            return MAPPER.writeValueAsString(value);
        } catch (Exception e) {
            throw new BizException(ErrorCode.SYSTEM_ERROR, "JSON 序列化失败", e);
        }
    }

    public static <T> T fromJson(String json, Class<T> type) {
        if (json == null || json.isBlank()) {
            return null;
        }
        try {
            return MAPPER.readValue(json, type);
        } catch (Exception e) {
            throw new BizException(ErrorCode.SYSTEM_ERROR, "JSON 反序列化失败", e);
        }
    }
}
