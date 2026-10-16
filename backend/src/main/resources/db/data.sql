-- =============================================================================
-- SmartLib 初始化数据（**H2 MODE=MySQL 与 MySQL 8.0 共用同一份**）
--
--   设计原则与 schema.sql 一致：只使用两种数据库都支持的语法子集，从而
--   消除"本地演示数据与部署数据不一致"的风险。具体约束：
--     * 时间表达式统一用 NOW() / TIMESTAMPADD(DAY, n, NOW())
--       （实测 H2 的 MySQL 模式不接受 DATE_ADD(..., INTERVAL ...) 形式，
--       而 TIMESTAMPADD 是两者都支持的标准函数）
--     * 字符串拼接统一用 CONCAT()，绝不用 ||（MySQL 默认把 || 当逻辑或）
--     * 不使用 SET NAMES / ALTER DATABASE 等会话级语句
--     * 关联插入用 INSERT ... SELECT ... JOIN，不写死自增 id，
--       避免不同数据库自增序列行为差异
--
--   deploy/sql/02-seed.sql 是本文件的副本，改动后请同步（CI 有校验脚本）。
-- =============================================================================

-- ---------------------------- 分类 -------------------------------------------
insert into category (code, name, parent_id, sort_no) values
 ('TP', '计算机与信息技术', null, 1),
 ('O1', '数学', null, 2),
 ('F2', '经济管理', null, 3),
 ('I2', '中国文学', null, 4),
 ('B0', '哲学宗教', null, 5),
 ('H3', '外语', null, 6);

-- ---------------------------- 读者 -------------------------------------------
insert into reader (card_no, name, type, dept, credit_score, status, created_at, updated_at) values
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
 ('T001003', '张慧敏', 'TEACHER',   '经济管理学院',           100, 'ACTIVE', NOW(), NOW()),
 ('2021009', '钱defaults', 'UNDERGRAD', '计算机科学与技术学院', 100, 'FROZEN', NOW(), NOW());

-- ---------------------------- 书目 -------------------------------------------
insert into book (isbn, title, author, publisher, category_id, publish_year, price, tags, summary, shelf_location, total_copies, created_at, updated_at) values
 ('9787111213826', '深入理解Java虚拟机', '周志明', '机械工业出版社', (select id from category where code='TP'), 2019, 129.00, 'Java,JVM,性能调优,虚拟机', '从内存模型、垃圾回收到编译优化的 JVM 系统级讲解，是 Java 工程师进阶必读。', '三楼东区 TP312/1287', 5, NOW(), NOW()),
 ('9787111544937', 'Java并发编程实战', 'Brian Goetz', '机械工业出版社', (select id from category where code='TP'), 2017, 89.00, 'Java,并发,多线程,线程安全', 'Java 并发领域经典，系统讲解线程安全、锁、并发容器与线程池。', '三楼东区 TP312/1321', 4, NOW(), NOW()),
 ('9787115428028', '计算机网络：自顶向下方法', 'James F. Kurose', '人民邮电出版社', (select id from category where code='TP'), 2018, 128.00, '计算机网络,网络协议,自顶向下,TCP', '以应用层为起点自顶向下讲解网络原理，配套实验丰富。', '三楼东区 TP393/771', 6, NOW(), NOW()),
 ('9787111213827', '数据库系统概念', 'Abraham Silberschatz', '机械工业出版社', (select id from category where code='TP'), 2021, 138.00, '数据库,SQL,事务,索引', '数据库领域教科书，覆盖关系模型、事务、并发控制与恢复。', '三楼东区 TP311/552', 3, NOW(), NOW()),
 ('9787111602583', '算法导论', 'Thomas H. Cormen', '机械工业出版社', (select id from category where code='TP'), 2013, 158.00, '算法,数据结构,动态规划,图论', '算法领域的权威教材，覆盖排序、图算法、动态规划与 NP 完全性。', '三楼东区 TP301/233', 4, NOW(), NOW()),
 ('9787111558422', '设计模式：可复用面向对象软件的基础', 'Erich Gamma', '机械工业出版社', (select id from category where code='TP'), 2019, 79.00, '设计模式,面向对象,重构,架构', 'GoF 二十三种设计模式的奠基之作。', '三楼东区 TP311/566', 3, NOW(), NOW()),
 ('9787111641247', '重构：改善既有代码的设计', 'Martin Fowler', '机械工业出版社', (select id from category where code='TP'), 2019, 118.00, '重构,代码质量,设计模式,单元测试', '系统讲解如何在保证行为不变的前提下改善代码结构。', '三楼东区 TP311/571', 3, NOW(), NOW()),
 ('9787115546081', '深入理解计算机系统', 'Randal E. Bryant', '机械工业出版社', (select id from category where code='TP'), 2016, 139.00, '计算机系统,汇编,缓存,链接', '从程序员视角贯穿硬件与系统软件的经典教材。', '三楼东区 TP303/214', 4, NOW(), NOW()),
 ('9787121362842', 'Elasticsearch实战', 'Radu Gheorghe', '电子工业出版社', (select id from category where code='TP'), 2020, 99.00, 'Elasticsearch,全文检索,倒排索引,搜索引擎', '讲解 ES 的索引原理、查询 DSL 与集群运维实践。', '三楼东区 TP391/88', 2, NOW(), NOW()),
 ('9787115473899', 'Redis设计与实现', '黄健宏', '机械工业出版社', (select id from category where code='TP'), 2018, 89.00, 'Redis,缓存,数据结构,持久化', '从源码角度讲解 Redis 的内部数据结构与持久化机制。', '三楼东区 TP311/580', 5, NOW(), NOW()),
 ('9787111407010', '高性能MySQL', 'Baron Schwartz', '机械工业出版社', (select id from category where code='TP'), 2013, 128.00, 'MySQL,索引优化,慢查询,分库分表', 'MySQL 性能优化的权威指南，含索引、查询优化与复制。', '三楼东区 TP311/490', 3, NOW(), NOW()),
 ('9787115293800', 'Spring实战', 'Craig Walls', '人民邮电出版社', (select id from category where code='TP'), 2020, 108.00, 'Spring,SpringBoot,IoC,AOP', 'Spring 全家桶的实践指南。', '三楼东区 TP312/1402', 5, NOW(), NOW()),
 ('9787115476272', '数学分析新讲', '张筑生', '北京大学出版社', (select id from category where code='O1'), 2018, 68.00, '数学分析,极限,微积分,级数', '面向初学者的数学分析教材，讲解细致。', '四楼西区 O17/312', 6, NOW(), NOW()),
 ('9787040396638', '高等代数', '北京大学数学系', '高等教育出版社', (select id from category where code='O1'), 2019, 45.00, '高等代数,线性代数,矩阵,多项式', '国内高校广泛采用的代数教材。', '四楼西区 O15/206', 8, NOW(), NOW()),
 ('9787040238969', '概率论与数理统计', '盛骤', '高等教育出版社', (select id from category where code='O1'), 2018, 42.00, '概率论,数理统计,随机变量,假设检验', '工科概率统计经典教材。', '四楼西区 O21/188', 10, NOW(), NOW()),
 ('9787115473898', '离散数学及其应用', 'Kenneth H. Rosen', '机械工业出版社', (select id from category where code='O1'), 2019, 99.00, '离散数学,图论,数理逻辑,组合数学', '计算机专业离散数学主流教材。', '四楼西区 O158/97', 4, NOW(), NOW()),
 ('9787111631170', '经济学原理', 'N. Gregory Mankiw', '机械工业出版社', (select id from category where code='F2'), 2020, 128.00, '经济学,微观经济,宏观经济,供需', '全球广泛使用的经济学入门教材。', '二楼南区 F0/433', 5, NOW(), NOW()),
 ('9787300287651', '管理学原理', '斯蒂芬·罗宾斯', '中国人民大学出版社', (select id from category where code='F2'), 2019, 79.00, '管理学,组织行为,决策,领导力', '管理学入门经典。', '二楼南区 C93/288', 4, NOW(), NOW()),
 ('9787020002207', '红楼梦', '曹雪芹', '人民文学出版社', (select id from category where code='I2'), 2018, 59.70, '古典文学,小说,四大名著,清代', '中国古典小说的巅峰之作。', '五楼北区 I242/77', 10, NOW(), NOW()),
 ('9787020008735', '平凡的世界', '路遥', '人民文学出版社', (select id from category where code='I2'), 2017, 108.00, '当代文学,长篇小说,现实主义,茅盾文学奖', '描写中国城乡社会变迁的当代长篇小说。', '五楼北区 I247/1929', 6, NOW(), NOW()),
 ('9787506365437', '活着', '余华', '作家出版社', (select id from category where code='I2'), 2012, 35.00, '当代文学,小说,人生,苦难', '讲述普通人在时代洪流中的生存与坚韧。', '五楼北区 I247/1688', 8, NOW(), NOW()),
 ('9787108009821', '万历十五年', '黄仁宇', '生活·读书·新知三联书店', (select id from category where code='I2'), 2015, 45.00, '历史,明史,大历史观,传记', '以 1587 年为切口剖析明代政治与社会结构。', '五楼北区 K248/120', 4, NOW(), NOW()),
 ('9787100006668', '西方哲学史', '伯特兰·罗素', '商务印书馆', (select id from category where code='B0'), 2016, 88.00, '哲学,哲学史,西方思想,认识论', '罗素撰写的西方哲学通史。', '六楼东区 B5/64', 3, NOW(), NOW()),
 ('9787544766333', '新概念英语', 'L.G. Alexander', '外语教学与研究出版社', (select id from category where code='H3'), 2018, 69.00, '英语,外语学习,语法,词汇', '经典英语学习教材。', '五楼南区 H31/1024', 12, NOW(), NOW());

-- ---------------------------- 库存 -------------------------------------------
-- 库存初始化：初始可借数 = 总复本数。全部通过 book 表关联，避免写死 id。
insert into book_stock (book_id, total_copies, available_copies, locked_copies, version, updated_at)
select id, total_copies, total_copies, 0, 0, NOW() from book;

-- ---------------------------- 借阅流水（演示"我的借阅"与逾期/续借） ----------
-- 陈嘉禾：正常在借 2 册
insert into borrow_record (reader_id, book_id, borrow_at, due_at, return_at, renew_count, status, fine, created_at, updated_at)
select r.id, b.id, TIMESTAMPADD(DAY, -5, NOW()), TIMESTAMPADD(DAY, 25, NOW()), null, 0, 'BORROWED', 0.00, NOW(), NOW()
from reader r, book b where r.card_no = '2021001' and b.isbn = '9787111213826';

insert into borrow_record (reader_id, book_id, borrow_at, due_at, return_at, renew_count, status, fine, created_at, updated_at)
select r.id, b.id, TIMESTAMPADD(DAY, -3, NOW()), TIMESTAMPADD(DAY, 27, NOW()), null, 1, 'BORROWED', 0.00, NOW(), NOW()
from reader r, book b where r.card_no = '2021001' and b.isbn = '9787111544937';

-- 林思远：逾期未还 1 册（用于演示逾期限制与罚金）
insert into borrow_record (reader_id, book_id, borrow_at, due_at, return_at, renew_count, status, fine, created_at, updated_at)
select r.id, b.id, TIMESTAMPADD(DAY, -70, NOW()), TIMESTAMPADD(DAY, -40, NOW()), null, 0, 'OVERDUE', 8.00, NOW(), NOW()
from reader r, book b where r.card_no = '2021002' and b.isbn = '9787115428028';

-- 周雨桐：已归还 1 册（历史记录）
insert into borrow_record (reader_id, book_id, borrow_at, due_at, return_at, renew_count, status, fine, created_at, updated_at)
select r.id, b.id, TIMESTAMPADD(DAY, -60, NOW()), TIMESTAMPADD(DAY, -30, NOW()), TIMESTAMPADD(DAY, -33, NOW()), 0, 'RETURNED', 0.00, NOW(), NOW()
from reader r, book b where r.card_no = '2021003' and b.isbn = '9787506365437';

-- 逾期与在借导致的库存占用：把对应书的可借数扣掉，保证库存与流水一致
update book_stock set available_copies = available_copies - 1, updated_at = NOW()
where book_id in (select id from book where isbn in ('9787111213826', '9787111544937', '9787115428028'));

-- ---------------------------- 预约队列 ---------------------------------------
-- 郑清越预约《算法导论》（该书仍有库存，此数据仅用于展示队列结构）
insert into reservation (reader_id, book_id, queue_no, status, ready_at, expire_at, created_at, updated_at)
select r.id, b.id, 1, 'WAITING', null, null, NOW(), NOW()
from reader r, book b where r.card_no = '2023007' and b.isbn = '9787111602583';

-- ---------------------------- 库存流水 ---------------------------------------
-- 与上面三次库存扣减对应，保证 sum(delta) 与库存变化一致（对账 SQL 见 docs/04）
insert into stock_ledger (book_id, op_type, delta, before_qty, after_qty, biz_no, strategy, trace_id, created_at)
select b.id, 'DEDUCT', -1, b.total_copies, b.total_copies - 1, CONCAT('SEED-', b.isbn), 'INIT', 'seed', NOW()
from book b where b.isbn = '9787111213826';

insert into stock_ledger (book_id, op_type, delta, before_qty, after_qty, biz_no, strategy, trace_id, created_at)
select b.id, 'DEDUCT', -1, b.total_copies, b.total_copies - 1, CONCAT('SEED2-', b.isbn), 'INIT', 'seed', NOW()
from book b where b.isbn = '9787111544937';

insert into stock_ledger (book_id, op_type, delta, before_qty, after_qty, biz_no, strategy, trace_id, created_at)
select b.id, 'DEDUCT', -1, b.total_copies, b.total_copies - 1, CONCAT('SEED3-', b.isbn), 'INIT', 'seed', NOW()
from book b where b.isbn = '9787115428028';

-- ---------------------------- 借阅规则 ---------------------------------------
-- 目标：改规则不发版。reader_type='*' 为兜底，具体类型优先。
insert into rule_config (rule_key, reader_type, rule_value, remark, version, updated_at) values
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
