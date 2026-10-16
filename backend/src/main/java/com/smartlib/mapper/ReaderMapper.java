package com.smartlib.mapper;

import java.util.List;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import com.smartlib.domain.dto.NamedCount;
import com.smartlib.domain.entity.Reader;

/**
 * 读者 Mapper。
 */
public interface ReaderMapper {

    @Insert("""
            insert into reader(card_no, name, type, dept, credit_score, status, created_at, updated_at)
            values(#{cardNo}, #{name}, #{type}, #{dept}, #{creditScore}, #{status}, now(), now())
            """)
    @Options(useGeneratedKeys = true, keyProperty = "id")
    int insert(Reader reader);

    @Select("select * from reader where id = #{id}")
    Reader findById(@Param("id") Long id);

    @Select("select * from reader where card_no = #{cardNo}")
    Reader findByCardNo(@Param("cardNo") String cardNo);

    /**
     * 信用分变更。使用 {@code greatest/least} 夹紧到 [0,100]，
     * 避免并发扣分把分数打成负数（H2 MODE=MySQL 与 MySQL 均支持这两个函数）。
     */
    @Update("""
            update reader
               set credit_score = least(100, greatest(0, credit_score + #{delta})), updated_at = now()
             where id = #{id}
            """)
    int addCreditScore(@Param("id") Long id, @Param("delta") int delta);

    @Select("select * from reader where status = 'ACTIVE' order by id asc limit #{limit} offset #{offset}")
    List<Reader> selectPage(@Param("offset") int offset, @Param("limit") int limit);

    /**
     * 读者偏好分类：从借阅历史中统计出现频次最高的分类，作为推荐的画像输入。
     */
    @Select("""
            select c.name as name, count(1) as cnt
              from borrow_record br
              join book b on b.id = br.book_id
              join category c on c.id = b.category_id
             where br.reader_id = #{readerId}
             group by c.name
             order by count(1) desc
             limit #{limit}
            """)
    List<NamedCount> findPreferredCategories(@Param("readerId") Long readerId, @Param("limit") int limit);
}
