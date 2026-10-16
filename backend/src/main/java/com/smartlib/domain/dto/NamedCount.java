package com.smartlib.domain.dto;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 分组统计读模型。
 *
 * <p>本类由 scripts/tools/gen_entities.py 生成，请勿手工修改字段：
 * 需要调整字段请修改生成器中的字段清单后重新生成（与
 * backend/src/main/resources/db/schema.sql 一一对应）。
 */
public class NamedCount {

    private String name;
    private Integer cnt;

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public Integer getCnt() {
        return cnt;
    }

    public void setCnt(Integer cnt) {
        this.cnt = cnt;
    }

    @Override
    public String toString() {
        return "NamedCount{"
                + "name=" + name + ", "
                + "cnt=" + cnt + ", "
                + '}';
    }
}
