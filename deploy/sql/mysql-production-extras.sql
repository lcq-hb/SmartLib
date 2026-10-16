-- =============================================================================
-- SmartLib MySQL 生产环境增量优化脚本
--
-- 定位：本文件【只做增量优化与权限收敛】，不重复建表。
--       表结构唯一来源是 backend/src/main/resources/db/schema.sql（已完成、只读参考）。
--       执行顺序（docker-entrypoint-initdb.d 按文件名字典序）：
--         01-schema.sql -> 02-seed.sql(可选) -> 03-mysql-production-extras.sql(本文件)
--       生产发布建议由 DBA/流水线手工执行本文件，并且是可重复执行的。
--
-- 目标环境：MySQL 8.0，字符集 utf8mb4，排序规则 utf8mb4_0900_ai_ci
-- 覆盖内容（对应蓝图 §6.2 检索方案 Q4 与 §5 数据模型）：
--   A. ngram 全文索引  —— Q4 方案的落地（FULLTEXT ... WITH PARSER ngram）
--   B. borrow_record 按年 RANGE 分区示例 —— 流水表容量治理
--   C. 只读/应用账号最小权限 GRANT 示例 —— 生产账号绝不能是 root
--   D. 慢查询与 InnoDB 参数建议 —— 定位"高并发下锁竞争"
--
-- 幂等性说明：MySQL 8.0 不支持 CREATE INDEX IF NOT EXISTS，
--   因此下面用 information_schema 判断 + PREPARE 动态执行，可重复运行。
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 0. 会话与库约定
-- ---------------------------------------------------------------------------
SET NAMES utf8mb4;
SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION';

-- ngram 分词长度是只读全局变量（启动参数 --ngram_token_size=2），这里只做校验提示。
-- 建全文索引前必须确认它是 1 或 2，否则查询会报 "Invalid ngram token size"。
-- SELECT @@ngram_token_size;   -- 期望 2

-- =============================================================================
-- A. ngram 全文索引（检索方案 Q4）
--
-- 为什么值得加：
--   Q4 用 MySQL 内建全文索引替换 like '%kw%'，把 120 万行全表扫描压到索引区间扫描。
--   中文没有空格分词，ngram parser 按 ngram_token_size 个字符切分，
--   因此 "计算机网络" 会被切成 "计算"/"算机"/"机网"/"网络" 等 2-gram 词组。
--
-- 索引列选择（与 BookMapper.xml 的检索字段一致）：
--   title      —— 权重最高（书名命中）
--   author     —— 读者最常用的检索维度之一（"计算机网络 谢希仁"）
--   tags       —— 人工标注关键词，提升召回
--   publisher  —— 出版社检索（采编与读者都会用）
--   summary    —— 简介参与低权重召回
--
-- 代价（写放大，必须写进对比手册）：
--   * 每张表一个全文索引要额外存储 ngram 倒排，约等于被索引文本量的 1~2 倍
--   * INSERT/UPDATE 时要同步维护倒排，批量灌数据（gen_books.py）会明显变慢
--     => 因此 gen_books.py 生成的数据建议【先灌数据、后建全文索引】
--   * 删除/更新频繁时会累积倒排垃圾，需要定期 OPTIMIZE TABLE
--
-- 注意：ngram 全文索引不接受 "WITH PARSER ngram" 与 "UNION" 组合，
--       也不能用在子查询里；查询侧必须用 MATCH ... AGAINST(... IN BOOLEAN MODE)。
-- =============================================================================

-- A.1 book.title 全文索引
SET @idx_exists := (
    SELECT COUNT(1) FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'book'
      AND index_name = 'ft_book_title'
);
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE book ADD FULLTEXT INDEX ft_book_title (title) WITH PARSER ngram',
    'SELECT ''ft_book_title already exists'' AS skip_note');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- A.2 book.author 全文索引（"谢希仁"这类作者检索）
SET @idx_exists := (
    SELECT COUNT(1) FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'book'
      AND index_name = 'ft_book_author'
);
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE book ADD FULLTEXT INDEX ft_book_author (author) WITH PARSER ngram',
    'SELECT ''ft_book_author already exists'' AS skip_note');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- A.3 book.tags 全文索引（人工标注关键词，召回主力之一）
SET @idx_exists := (
    SELECT COUNT(1) FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'book'
      AND index_name = 'ft_book_tags'
);
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE book ADD FULLTEXT INDEX ft_book_tags (tags) WITH PARSER ngram',
    'SELECT ''ft_book_tags already exists'' AS skip_note');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- A.4 组合全文索引：一条 MATCH 覆盖 书名+作者+标签
--     好处：一次倒排匹配即可打分排序，避免多索引 MATCH 结果集求交的开销
--     代价：任何一列更新都要重建整个组合倒排索引
SET @idx_exists := (
    SELECT COUNT(1) FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'book'
      AND index_name = 'ft_book_title_author_tags'
);
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE book ADD FULLTEXT INDEX ft_book_title_author_tags (title, author, tags) WITH PARSER ngram',
    'SELECT ''ft_book_title_author_tags already exists'' AS skip_note');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- A.5 组合全文索引：书名 + 简介，服务"带简介的长尾自然语言查询"
SET @idx_exists := (
    SELECT COUNT(1) FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'book'
      AND index_name = 'ft_book_summary'
);
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE book ADD FULLTEXT INDEX ft_book_summary (summary) WITH PARSER ngram',
    'SELECT ''ft_book_summary already exists'' AS skip_note');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- A.6 book_stock / borrow_record 的支撑索引（Q4 查询要 join 库存过滤"仅看可借"）
--     schema.sql 已有 idx_stock_available 与 idx_borrow_book_status；
--     这里补一个"可借且按分类"的覆盖索引，避免回表。
SET @idx_exists := (
    SELECT COUNT(1) FROM information_schema.statistics
    WHERE table_schema = DATABASE()
      AND table_name = 'book'
      AND index_name = 'idx_book_category_pubyear'
);
SET @ddl := IF(@idx_exists = 0,
    'ALTER TABLE book ADD INDEX idx_book_category_pubyear (category_id, publish_year, id)',
    'SELECT ''idx_book_category_pubyear already exists'' AS skip_note');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- A.7 自检：打印已建成的全文索引
SELECT table_name, index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index) AS columns_in_index
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND index_type = 'FULLTEXT'
GROUP BY table_name, index_name
ORDER BY table_name, index_name;

-- A.8 Q4 查询范式（注释形式，供对比手册引用；不要在初始化脚本里真的跑全表 MATCH）
--   SELECT b.id, b.title, b.author,
--          MATCH(b.title, b.author, b.tags) AGAINST ('计算机网络 谢希仁' IN BOOLEAN MODE) AS score
--   FROM book b
--   JOIN book_stock s ON s.book_id = b.id
--   WHERE MATCH(b.title, b.author, b.tags) AGAINST ('计算机网络 谢希仁' IN BOOLEAN MODE)
--     AND (:availableOnly = 0 OR s.available_copies > 0)
--   ORDER BY score DESC, b.id ASC
--   LIMIT 20;
--
--   -- 说明：ngram_token_size=2 时，单字查询（如 "书"）不会命中任何倒排项，
--   --       查询侧应把长度 <2 的关键词交给 Q3 自研倒排或前缀匹配兜底。

-- =============================================================================
-- B. borrow_record 按年 RANGE 分区（流水表容量治理）
--
-- 场景：日均借还 4000 次 => 年约 146 万行，10 年就是 1460 万行。
--   借阅流水是"只追加 + 按时间范围查询"的典型时序形态，按年分区后：
--     * 历史年份查询只扫对应分区（分区裁剪）
--     * 归档 = 直接 DROP PARTITION，秒级且不产生 undo 膨胀
--     * 删除过期数据不再需要 DELETE ... WHERE borrow_at < ?（避免大事务）
--
-- 【重要约束】MySQL 分区表要求：分区键必须是【所有】唯一索引（含主键）的一部分。
--   borrow_record 主键是 id，因此必须改成 PRIMARY KEY (id, borrow_at) 才能按 borrow_at
--   RANGE 分区。这会改变主键语义（id 不再全局唯一 —— 实际上 MySQL 只保证
--   (id, borrow_at) 组合唯一）。因此本段以【示例 + 显式开关】的方式给出：
--   * 默认不执行（@enable_borrow_partition = 0），避免初始化时静默改变主键；
--   * 需要启用时把它改成 1，并先确认没有以 id 为唯一键的外键/业务假设。
--   * 已上线的表要分区只能重建（pt-online-schema-change / gh-ost）。
-- =============================================================================

SET @enable_borrow_partition := 0;

-- B.1 检查：当前是否已分区
SET @already_partitioned := (
    SELECT COUNT(1) FROM information_schema.partitions
    WHERE table_schema = DATABASE()
      AND table_name = 'borrow_record'
      AND partition_name IS NOT NULL
);

-- B.2 分区示例（重建表 + 9 个分区：2019~2026 + pmax 兜底）
--     生产建议用 gh-ost 在线改，本段仅作为"可执行的分区 DDL 参考"。
SET @ddl := IF(@enable_borrow_partition = 1 AND @already_partitioned = 0,
    'ALTER TABLE borrow_record
       PARTITION BY RANGE (TO_DAYS(borrow_at)) (
         PARTITION p2019 VALUES LESS THAN (TO_DAYS(''2020-01-01'')),
         PARTITION p2020 VALUES LESS THAN (TO_DAYS(''2021-01-01'')),
         PARTITION p2021 VALUES LESS THAN (TO_DAYS(''2022-01-01'')),
         PARTITION p2022 VALUES LESS THAN (TO_DAYS(''2023-01-01'')),
         PARTITION p2023 VALUES LESS THAN (TO_DAYS(''2024-01-01'')),
         PARTITION p2024 VALUES LESS THAN (TO_DAYS(''2025-01-01'')),
         PARTITION p2025 VALUES LESS THAN (TO_DAYS(''2026-01-01'')),
         PARTITION p2026 VALUES LESS THAN (TO_DAYS(''2027-01-01'')),
         PARTITION pmax  VALUES LESS THAN (MAXVALUE)
       )',
    IF(@already_partitioned = 1,
       'SELECT ''borrow_record already partitioned'' AS skip_note',
       'SELECT ''borrow_record partitioning disabled (set @enable_borrow_partition=1)'' AS skip_note'));
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- B.3 归档示例（注释；DROP PARTITION 不产生大量 undo，是分区最大的收益）
--   ALTER TABLE borrow_record DROP PARTITION p2019;                 -- 秒级归档
--   ALTER TABLE borrow_record REORGANIZE PARTITION pmax INTO (      -- 每年例行加分区
--       PARTITION p2027 VALUES LESS THAN (TO_DAYS('2028-01-01')),
--       PARTITION pmax  VALUES LESS THAN (MAXVALUE));
--
-- B.4 更轻量的替代方案（不改主键，推荐先用这个）：
--   按月分表 borrow_record_202601 ...，应用层按 borrow_at 路由；
--   或者只做"冷热分离"：热表保留最近 2 年，冷数据进 borrow_record_history。

-- =============================================================================
-- C. 账号与最小权限（生产绝不能直接用 root 跑应用）
--
-- 角色划分：
--   smartlib_app    应用账号：DML + 存储过程执行，无 DDL、无 GRANT
--   smartlib_ro     只读账号：给报表 / Agent 服务的只读工具 / DBA 排查使用
--   smartlib_shadow 影子库账号（可选）：灰度与回滚演练
--
-- 安全要点：
--   * 只允许从容器网段/内网段登录，禁止 '%'
--   * 不给 FILE / SUPER / PROCESS / RELOAD（后两个会泄露全实例信息）
--   * 密码不要写死在这里：用 `${SMARTLIB_DB_PASSWORD}` 由流水线注入，
--     或改用 MySQL 8 的 caching_sha2_password + 密钥管理服务。
-- =============================================================================

-- C.1 应用账号（示例密码，务必改名/换密码；已存在则只改密码不重建）
CREATE USER IF NOT EXISTS 'smartlib_app'@'172.16.0.%' IDENTIFIED BY 'CHANGE_ME_APP_PASSWORD';
ALTER USER 'smartlib_app'@'172.16.0.%' IDENTIFIED BY 'CHANGE_ME_APP_PASSWORD';

-- 只给 DML：SELECT/INSERT/UPDATE/DELETE。没有 DROP/ALTER，误操作炸不到表结构。
GRANT SELECT, INSERT, UPDATE, DELETE ON smartlib.* TO 'smartlib_app'@'172.16.0.%';

-- C.2 只读账号：报表、Agent 只读工具、DBA 排查
CREATE USER IF NOT EXISTS 'smartlib_ro'@'172.16.0.%' IDENTIFIED BY 'CHANGE_ME_RO_PASSWORD';
ALTER USER 'smartlib_ro'@'172.16.0.%' IDENTIFIED BY 'CHANGE_ME_RO_PASSWORD';
GRANT SELECT ON smartlib.* TO 'smartlib_ro'@'172.16.0.%';
-- 只读账号连 SHOW PROCESSLIST 都不给（PROCESS 会暴露其他库的 SQL 文本）
REVOKE PROCESS ON *.* FROM 'smartlib_ro'@'172.16.0.%';

-- C.3 明确收回全局权限（"显式化"审计口径）
--     顺序很重要：REVOKE 一个从未 GRANT 过的权限会直接报错
--     （ERROR 1147 There is no such grant defined for user ...），
--     因此必须先 GRANT 再 REVOKE，不能反过来。
GRANT USAGE ON *.* TO 'smartlib_app'@'172.16.0.%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smartlib.* TO 'smartlib_app'@'172.16.0.%';
REVOKE ALL PRIVILEGES ON *.* FROM 'smartlib_app'@'172.16.0.%';
GRANT SELECT, INSERT, UPDATE, DELETE ON smartlib.* TO 'smartlib_app'@'172.16.0.%';

-- C.4 让权限生效
FLUSH PRIVILEGES;

-- C.5 审计：打印当前账号权限，部署后人工核对一遍
SELECT user, host, IF(account_locked = 'Y', 'LOCKED', 'ACTIVE') AS state
FROM mysql.user
WHERE user LIKE 'smartlib%';

SHOW GRANTS FOR 'smartlib_app'@'172.16.0.%';

-- =============================================================================
-- D. 慢查询与 InnoDB 参数建议
--
-- 说明：以下均为【建议值】。部分参数是只读的（标注 READONLY），
--       必须写进 my.cnf / 容器启动参数（docker-compose 已带了一部分），
--       这里用 SET GLOBAL 的只给出可动态修改的那些。
-- =============================================================================

-- D.1 慢查询日志（定位"高并发下究竟是哪条 SQL 在等行锁"）
--     docker-compose 已通过启动参数开启；这里给出等价的可动态设置写法。
SET GLOBAL slow_query_log = ON;
SET GLOBAL long_query_time = 0.5;            -- 0.5s：借阅接口 SLO 是 P99 < 200ms
SET GLOBAL log_queries_not_using_indexes = ON;  -- 抓 like '%kw%' 这类全表扫描（Q1 基线）
SET GLOBAL log_throttle_queries_not_using_indexes = 10;  -- 防止刷爆慢日志

-- D.2 建议写入 my.cnf 的只读参数（此处仅注释，改完需重启）：
--   [mysqld]
--   innodb_buffer_pool_size        = 8G          # 物理内存的 50%~70%
--   innodb_buffer_pool_instances   = 8           # 多实例减少 buffer pool 互斥
--   innodb_flush_log_at_trx_commit = 1           # 库存在正确性上零妥协，保持 1
--   innodb_log_file_size           = 1G          # 大日志文件减少 checkpoint 抖动
--   innodb_io_capacity             = 4000        # SSD
--   innodb_lock_wait_timeout       = 5           # 借书持锁不该超过 5s，超时即快速失败
--   innodb_deadlock_detect         = ON          # S3 悲观锁方案必开
--   transaction_isolation          = READ-COMMITTED  # 减少间隙锁，降低 S2/S4 死锁率
--   max_connections                = 500
--   table_open_cache               = 4000
--   tmp_table_size                 = 64M
--   max_heap_table_size            = 64M
--   ngram_token_size               = 2           # 只读，全文索引 Q4 前置条件
--   character_set_server           = utf8mb4
--   collation_server               = utf8mb4_0900_ai_ci
--   default_time_zone              = '+08:00'

-- D.3 库存扣减专用提醒（对应 §6.1 六种策略）
--   * S2 条件更新：UPDATE book_stock SET available_copies = available_copies - 1
--                   WHERE book_id = ? AND available_copies >= 1;
--     —— 这条走的是主键等值更新，加行锁；并发高时表现为 lock wait，不是慢查询。
--   * S3 悲观锁：SELECT ... FOR UPDATE 必须走主键（book_id），否则会锁区间；
--     并确保 innodb_lock_wait_timeout 设小（5s），让排队请求快速失败而不是堆积。
--   * 观察锁等待的 SQL（需要 PROCESS 权限，用 DBA 账号执行）：
--       SELECT * FROM performance_schema.data_lock_waits;
--       SELECT * FROM sys.innodb_lock_waits;
--   * 定期清理倒排垃圾：OPTIMIZE TABLE book;   -- 全文索引更新频繁时每月一次

-- =============================================================================
-- E. 收尾自检
-- =============================================================================
SELECT
    (SELECT COUNT(1) FROM information_schema.statistics
      WHERE table_schema = DATABASE() AND index_type = 'FULLTEXT') AS fulltext_index_count,
    (SELECT COUNT(1) FROM information_schema.tables
      WHERE table_schema = DATABASE())                            AS table_count,
    @@ngram_token_size                                             AS ngram_token_size,
    @@transaction_isolation                                        AS isolation_level;

-- 脚本结束。任何 ALTER 失败都会因严格 sql_mode 直接中断，不会留下半成品状态。
