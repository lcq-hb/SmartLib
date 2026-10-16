package com.smartlib.mapper;

import java.util.List;

import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import com.smartlib.domain.entity.RuleConfig;

/**
 * 借阅规则配置 Mapper。
 *
 * <p>规则查表时用 {@code case when} 让"读者类型精确匹配"排在通配 '*' 前面，
 * 一次查询就能拿到优先级正确的规则，避免在 Java 侧做两轮查询。
 */
public interface RuleConfigMapper {

    @Select("select * from rule_config order by rule_key asc, reader_type asc")
    List<RuleConfig> findAll();

    @Select("""
            select * from rule_config
             where rule_key = #{ruleKey} and reader_type in (#{readerType}, '*')
             order by case when reader_type = '*' then 1 else 0 end asc
            """)
    List<RuleConfig> findEffective(@Param("ruleKey") String ruleKey, @Param("readerType") String readerType);

    @Update("""
            update rule_config
               set rule_value = #{ruleValue}, remark = #{remark}, version = version + 1, updated_at = now()
             where rule_key = #{ruleKey} and reader_type = #{readerType}
            """)
    int updateValue(@Param("ruleKey") String ruleKey,
                    @Param("readerType") String readerType,
                    @Param("ruleValue") String ruleValue,
                    @Param("remark") String remark);
}
