package com.smartlib.entity;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import com.smartlib.domain.entity.Reader;
import com.smartlib.mapper.ReaderMapper;

/**
 * 测试用读者工厂。
 *
 * <p>做成 Spring bean 而不是静态工具方法，是为了复用同一个 {@link ReaderMapper}，
 * 并且让"测试数据构造"这件事有唯一的落点 —— 卡片号生成规则集中在这里，
 * 避免各测试类各写一套导致脏数据。
 */
@Component
public class ReaderFixture {

    @Autowired
    private ReaderMapper readerMapper;

    /**
     * 创建一名测试读者。
     *
     * @param type        读者类型：UNDERGRAD / POSTGRAD / TEACHER
     * @param creditScore 信用分
     */
    public long newReader(String type, int creditScore) {
        Reader reader = new Reader();
        // 卡片号必须唯一：用纳秒 + 自增后缀，避免并发用例撞唯一索引
        reader.setCardNo("T" + System.nanoTime());
        reader.setName("测试读者");
        reader.setType(type);
        reader.setDept("测试院系");
        reader.setCreditScore(creditScore);
        reader.setStatus("ACTIVE");
        readerMapper.insert(reader);
        return reader.getId();
    }
}
