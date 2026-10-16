package com.smartlib.domain.enums;

/**
 * 读者类型，决定可借册数、借期与续借次数（具体数值在 rule_config 表中配置）。
 *
 * <p>本枚举由 scripts/tools/gen_entities.py 生成。
 */
public enum ReaderType {
    UNDERGRAD,
    POSTGRAD,
    TEACHER;
}
