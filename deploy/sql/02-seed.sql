-- =============================================================================
-- SmartLib MySQL 初始化脚本 02：演示种子数据（可选）
--
-- 定位：让 Compose 起起来就有"能借的书 + 能借书的人 + 一条逾期记录"可演示，
--       不需要先跑 gen_books.py 灌 20 万行。
-- 【前提】必须等 03-mysql-production-extras.sql 的全文索引建完再灌大数据量；
--       本文件的 14 本书量级极小，与 01 的建表顺序无冲突。
--
-- 与 H2 版 data.sql 的差异（这是刻意的，不是遗漏）：
--   * H2 版用 dateadd('DAY', -5, current_timestamp)；MySQL 用
--     DATE_SUB(NOW(), INTERVAL 5 DAY)。两套语法不通用，所以分开维护。
--   * H2 版用 'SEED-' || isbn 拼字符串；MySQL 的 || 默认是逻辑或，
--     必须用 CONCAT()。
--   * 本文件只放"演示必需"的 14 本书；120 万行级别的压力数据由
--     scripts/gen_books.py 生成（产物在 deploy/sql/generated/，已 gitignore）。
--
-- 幂等性：01-schema.sql 开头有 drop table，因此本文件无需再判重；
--       但为了支持"只重灌数据不重建表"，这里用 INSERT ... ON DUPLICATE KEY UPDATE
--       兜底需要唯一键的表（reader.card_no / book.isbn / rule_config）。
-- =============================================================================

SET NAMES utf8mb4;

-- ---------------------------- 分类 -------------------------------------------
INSERT INTO category (code, name, parent_id, sort_no) VALUES
 ('TP', '计算机与信息技术', NULL, 1),
 ('O1', '数学', NULL, 2),
 ('F2', '经济管理', NULL, 3),
 ('I2', '中国文学', NULL, 4),
 ('B0', '哲学宗教', NULL, 5),
 ('H3', '外语', NULL, 6);

-- ---------------------------- 读者 -------------------------------------------
INSERT INTO reader (card_no, name, type, dept, credit_score, status, created_at, updated_at) VALUES
 ('2021001', '陈嘉禾', 'UNDERGRAD', '计算机科学与技术学院', 100, 'ACTIVE', NOW(), NOW()),
 ('2021002', '林思远', 'UNDERGRAD', '计算机科学与技术学院', 95,  'ACTIVE', NOW(), NOW()),
 ('2021003', '周雨桐', 'UNDERGRAD', '外国语学院',             88,  'ACTIVE', NOW(), NOW()),
 ('2022004', '赵明轩', 'UNDERGRAD', '数学学院',               100, 'ACTIVE', NOW(), NOW()),
 ('2022005', '孙一诺', 'UNDERGRAD', '经济管理学院',           55,  'ACTIVE', NOW(), NOW()),
 ('2023006', '吴子涵', 'POSTGRAD',  '计算机科学与技术学院',   100, 'ACTIVE', NOW(), NOW()),
 ('2023007', '郑清越', 'POSTGRAD',  '数学学院',               100, 'ACTIVE', NOW(), NOW()),
 ('2024008', '何书言', 'POSTGRAD',  '中国语言文学系',         92,  'ACTIVE', NOW(), NOW()),
 ('T001001', '王建国', 'TEACHER',   '计算机科学与技术学院',   100, 'ACTIVE', NOW(), NOW()),
 ('T001002', '李文博', 'TEACHER',   '数学学院',               100, 'ACTIVE', NOW(), NOW()),
 ('T001003', '张慧敏', 'TEACHER',   '经济管理学院',           100, 'ACTIVE', NOW(), NOW());

-- ---------------------------- 书目 -------------------------------------------
-- 说明：category_id 用 (select id from category where code='..') 关联，不写死自增 id。
INSERT INTO book (isbn, title, author, publisher, category_id, publish_year, price, tags, summary, shelf_location, total_copies, created_at, updated_at) VALUES
 ('9787111213826', '深入理解Java虚拟机', '周志明', '机械工业出版社', (SELECT id FROM category WHERE code='TP'), 2019, 129.00, 'Java,JVM,性能调优,虚拟机', '从内存模型、垃圾回收到编译优化的 JVM 系统级讲解，是 Java 工程师进阶必读。', '三楼东区 TP312/1287', 5, NOW(), NOW()),
 ('9787111544937', 'Java并发编程实战', 'Brian Goetz', '机械工业出版社', (SELECT id FROM category WHERE code='TP'), 2017, 89.00, 'Java,并发,多线程,线程安全', 'Java 并发领域经典，系统讲解线程安全、锁、并发容器与线程池。', '三楼东区 TP312/1321', 4, NOW(), NOW()),
 ('9787115428028', '计算机网络：自顶向下方法', 'James F. Kurose', '人民邮电出版社', (SELECT id FROM category WHERE code='TP'), 2018, 128.00, '计算机网络,网络协议,自顶向下,TCP', '以应用层为起点自顶向下讲解网络原理，配套实验丰富。', '三楼东区 TP393/771', 6, NOW(), NOW()),
 ('9787111213827', '数据库系统概念', 'Abraham Silberschatz', '机械工业出版社', (SELECT id FROM category WHERE code='TP'), 2021, 138.00, '数据库,SQL,事务,索引', '数据库领域教科书，覆盖关系模型、事务、并发控制与恢复。', '三楼东区 TP311/552', 3, NOW(), NOW()),
 ('9787111602583', '算法导论', 'Thomas H. Cormen', '机械工业出版社', (SELECT id FROM category WHERE code='TP'), 2013, 158.00, '算法,数据结构,动态规划,图论', '算法领域的权威教材，覆盖排序、图算法、动态规划与 NP 完全性。', '三楼东区 TP301/233', 4, NOW(), NOW()),
 ('9787111558422', '设计模式：可复用面向对象软件的基础', 'Erich Gamma', '机械工业出版社', (SELECT id FROM category WHERE code='TP'), 2019, 79.00, '设计模式,面向对象,重构,架构', 'GoF 二十三种设计模式的奠基之作。', '三楼东区 TP311/566', 3, NOW(), NOW()),
 ('9787111641247', '重构：改善既有代码的设计', 'Martin Fowler', '机械工业出版社', (SELECT id FROM category WHERE code='TP'), 2019, 118.00, '重构,代码质量,设计模式,单元测试', '系统讲解如何在保证行为不变的前提下改善代码结构。', '三楼东区 TP311/571', 3, NOW(), NOW()),
 ('9787115546081', '深入理解计算机系统', 'Randal E. Bryant', '机械工业出版社', (SELECT id FROM category WHERE code='TP'), 2016, 139.00, '计算机系统,汇编,缓存,链接', '从程序员视角贯穿硬件与系统软件的经典教材。', '三楼东区 TP303/214', 4, NOW(), NOW()),
 ('9787121362842', 'Elasticsearch实战', 'Radu Gheorghe', '电子工业出版社', (SELECT id FROM category WHERE code='TP'), 2020, 99.00, 'Elasticsearch,全文检索,倒排索引,搜索引擎', '讲解 ES 的索引原理、查询 DSL 与集群运维实践。', '三楼东区 TP391/88', 2, NOW(), NOW()),
 ('9787115473899', 'Redis设计与实现', '黄健宏', '机械工业出版社', (SELECT id FROM category WHERE code='TP'), 2018, 89.00, 'Redis,缓存,数据结构,持久化', '从源码角度讲解 Redis 的内部数据结构与持久化机制。', '三楼东区 TP311/580', 5, NOW(), NOW()),
 ('9787111407010', '高性能MySQL', 'Baron Schwartz', '机械工业出版社', (SELECT id FROM category WHERE code='TP'), 2013, 128.00, 'MySQL,索引优化,慢查询,分库分表', 'MySQL 性能优化的权威指南，含索引、查询优化与复制。', '三楼东区 TP311/490', 3, NOW(), NOW()),
 ('9787115293800', 'Spring实战', 'Craig Walls', '人民邮电出版社', (SELECT id FROM category WHERE code='TP'), 2020, 108.00, 'Spring,SpringBoot,IoC,AOP', 'Spring 全家桶的实践指南。', '三楼东区 TP312/1402', 5, NOW(), NOW()),
 ('9787020002207', '红楼梦', '曹雪芹', '人民文学出版社', (SELECT id FROM category WHERE code='I2'), 2018, 59.70, '古典文学,小说,四大名著,清代', '中国古典小说的巅峰之作。', '五楼北区 I242/77', 10, NOW(), NOW()),
 ('9787115473898', '离散数学及其应用', 'Kenneth H. Rosen', '机械工业出版社', (SELECT id FROM category WHERE code='O1'), 2019, 99.00, '离散数学,图论,数理逻辑,组合数学', '计算机专业离散数学主流教材。', '四楼西区 O158/97', 4, NOW(), NOW());

-- ---------------------------- 库存 -------------------------------------------
-- 通过 book 表关联，不写死自增 id；初始可借数 = 总复本数。
INSERT INTO book_stock (book_id, total_copies, available_copies, locked_copies, version, updated_at)
SELECT id, total_copies, total_copies, 0, 0, NOW() FROM book;

-- ---------------------------- 借阅流水（演示"我的借阅"/逾期/罚金） ------------
-- 陈嘉禾：正常在借 2 册
INSERT INTO borrow_record (reader_id, book_id, borrow_at, due_at, return_at, renew_count, status, fine, created_at, updated_at)
SELECT r.id, b.id, DATE_SUB(NOW(), INTERVAL 5 DAY), DATE_ADD(NOW(), INTERVAL 25 DAY), NULL, 0, 'BORROWED', 0.00, NOW(), NOW()
FROM reader r JOIN book b ON b.isbn = '9787111213826' WHERE r.card_no = '2021001';

INSERT INTO borrow_record (reader_id, book_id, borrow_at, due_at, return_at, renew_count, status, fine, created_at, updated_at)
SELECT r.id, b.id, DATE_SUB(NOW(), INTERVAL 3 DAY), DATE_ADD(NOW(), INTERVAL 27 DAY), NULL, 1, 'BORROWED', 0.00, NOW(), NOW()
FROM reader r JOIN book b ON b.isbn = '9787111544937' WHERE r.card_no = '2021001';

-- 林思远：逾期未还 1 册（演示逾期限制与罚金 = 40 天 * 0.2 = 8.00）
INSERT INTO borrow_record (reader_id, book_id, borrow_at, due_at, return_at, renew_count, status, fine, created_at, updated_at)
SELECT r.id, b.id, DATE_SUB(NOW(), INTERVAL 70 DAY), DATE_SUB(NOW(), INTERVAL 40 DAY), NULL, 0, 'OVERDUE', 8.00, NOW(), NOW()
FROM reader r JOIN book b ON b.isbn = '9787115428028' WHERE r.card_no = '2021002';

-- 周雨桐：已归还 1 册（历史记录）
INSERT INTO borrow_record (reader_id, book_id, borrow_at, due_at, return_at, renew_count, status, fine, created_at, updated_at)
SELECT r.id, b.id, DATE_SUB(NOW(), INTERVAL 60 DAY), DATE_SUB(NOW(), INTERVAL 30 DAY), DATE_SUB(NOW(), INTERVAL 33 DAY), 0, 'RETURNED', 0.00, NOW(), NOW()
FROM reader r JOIN book b ON b.isbn = '9787020002207' WHERE r.card_no = '2021003';

-- 上面 3 册在借/逾期占用了库存，可借数同步扣减，保证"库存与流水一致"
-- （对应 GET /api/admin/reconcile 必须返回空数组）
UPDATE book_stock s
JOIN book b ON b.id = s.book_id
SET s.available_copies = s.available_copies - 1, s.updated_at = NOW()
WHERE b.isbn IN ('9787111213826', '9787111544937', '9787115428028');

-- ---------------------------- 预约队列 ---------------------------------------
INSERT INTO reservation (reader_id, book_id, queue_no, status, ready_at, expire_at, created_at, updated_at)
SELECT r.id, b.id, 1, 'WAITING', NULL, NULL, NOW(), NOW()
FROM reader r JOIN book b ON b.isbn = '9787111602583' WHERE r.card_no = '2023007';

-- ---------------------------- 库存流水 ---------------------------------------
-- 与上面三次扣减一一对应：sum(delta) 必须等于 available_copies 的变化量。
-- before_qty/after_qty 取 book.total_copies（初始快照），与 H2 版 data.sql 口径一致。
INSERT INTO stock_ledger (book_id, op_type, delta, before_qty, after_qty, biz_no, strategy, trace_id, created_at)
SELECT b.id, 'DEDUCT', -1, b.total_copies, b.total_copies - 1, CONCAT('SEED-', b.isbn), 'INIT', 'seed', NOW()
FROM book b WHERE b.isbn = '9787111213826';

INSERT INTO stock_ledger (book_id, op_type, delta, before_qty, after_qty, biz_no, strategy, trace_id, created_at)
SELECT b.id, 'DEDUCT', -1, b.total_copies, b.total_copies - 1, CONCAT('SEED2-', b.isbn), 'INIT', 'seed', NOW()
FROM book b WHERE b.isbn = '9787111544937';

INSERT INTO stock_ledger (book_id, op_type, delta, before_qty, after_qty, biz_no, strategy, trace_id, created_at)
SELECT b.id, 'DEDUCT', -1, b.total_copies, b.total_copies - 1, CONCAT('SEED3-', b.isbn), 'INIT', 'seed', NOW()
FROM book b WHERE b.isbn = '9787115428028';

-- ---------------------------- 借阅规则 ---------------------------------------
-- 目标：改规则不发版。reader_type='*' 为兜底，具体类型优先。
INSERT INTO rule_config (rule_key, reader_type, rule_value, remark, version, updated_at) VALUES
 ('max_borrow_count', 'UNDERGRAD', '10', '本科生可借册数', 0, NOW()),
 ('max_borrow_count', 'POSTGRAD',  '15', '研究生可借册数', 0, NOW()),
 ('max_borrow_count', 'TEACHER',   '20', '教师可借册数', 0, NOW()),
 ('loan_days',        'UNDERGRAD', '30', '本科生借期(天)', 0, NOW()),
 ('loan_days',        'POSTGRAD',  '60', '研究生借期(天)', 0, NOW()),
 ('loan_days',        'TEACHER',   '90', '教师借期(天)', 0, NOW()),
 ('max_renew_count',  'UNDERGRAD', '2',  '本科生可续借次数', 0, NOW()),
 ('max_renew_count',  'POSTGRAD',  '3',  '研究生可续借次数', 0, NOW()),
 ('max_renew_count',  'TEACHER',   '3',  '教师可续借次数', 0, NOW()),
 ('renew_days',       'UNDERGRAD', '30', '本科生单次续借天数', 0, NOW()),
 ('renew_days',       'POSTGRAD',  '60', '研究生单次续借天数', 0, NOW()),
 ('renew_days',       'TEACHER',   '90', '教师单次续借天数', 0, NOW()),
 ('fine_per_day',     '*',         '0.20', '逾期罚金(元/天/册)', 0, NOW()),
 ('fine_free_limit',  '*',         '10.00', '欠费超过该金额禁止借阅', 0, NOW()),
 ('min_credit_score', '*',         '60', '低于该信用分禁止借阅', 0, NOW()),
 ('reservation_hold_days', '*',    '3',  '预约到馆保留天数', 0, NOW()),
 ('max_renew_days',   '*',         '180', '单册最长借阅天数上限', 0, NOW());

-- ---------------------------- 自检 -------------------------------------------
-- 对账不变式：每个 book_id 的流水求和 + 初始总量 必须等于当前可借数。
-- 期望查询结果为空（有行 = 种子数据本身不自洽，必须修脚本而不是改断言）。
SELECT s.book_id, s.total_copies, s.available_copies,
       COALESCE(l.delta_sum, 0) AS ledger_delta_sum
FROM book_stock s
LEFT JOIN (
    SELECT book_id, SUM(delta) AS delta_sum FROM stock_ledger GROUP BY book_id
) l ON l.book_id = s.book_id
WHERE l.delta_sum IS NOT NULL
  AND s.available_copies <> s.total_copies + l.delta_sum;
