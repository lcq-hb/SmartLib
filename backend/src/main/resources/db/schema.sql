-- =============================================================================
-- SmartLib 数据库结构
-- 目标：同时兼容 MySQL 8.0 与 H2 (MODE=MySQL)，因此只使用两者都支持的语法子集：
--   * 不使用 ENGINE / CHARSET / 行内 COMMENT / UNIQUE KEY 行内约束
--   * 唯一约束与普通索引统一用 CREATE [UNIQUE] INDEX 语句创建
--   * 表与字段说明用 -- 注释表达，MySQL 生产环境的额外优化见
--     deploy/sql/mysql-production-extras.sql（ngram 全文索引、分区、账号权限）
-- 说明：本项目所有库存扣减都必须经过 book_stock，任何方案都不允许无约束 UPDATE。
-- =============================================================================

drop table if exists stock_ledger;
drop table if exists idempotent_record;
drop table if exists reservation;
drop table if exists borrow_record;
drop table if exists book_stock;
drop table if exists book;
drop table if exists category;
drop table if exists reader;
drop table if exists rule_config;

-- -----------------------------------------------------------------------------
-- reader 读者
-- -----------------------------------------------------------------------------
create table reader (
    id            bigint auto_increment primary key,
    card_no       varchar(32)  not null,           -- 校园卡号，业务唯一键
    name          varchar(64)  not null,
    type          varchar(20)  not null,           -- UNDERGRAD / POSTGRAD / TEACHER
    dept          varchar(64),                     -- 院系，用于学科推荐
    credit_score  int          not null default 100, -- 信用分 0~100
    status        varchar(20)  not null default 'ACTIVE', -- ACTIVE / FROZEN
    created_at    datetime     not null,
    updated_at    datetime     not null
);

-- -----------------------------------------------------------------------------
-- category 图书分类（两级，parent_id 为 null 表示一级分类）
-- -----------------------------------------------------------------------------
create table category (
    id         bigint auto_increment primary key,
    code       varchar(32)  not null,              -- 中图法分类号，如 TP311
    name       varchar(64)  not null,
    parent_id  bigint,
    sort_no    int          not null default 0
);

-- -----------------------------------------------------------------------------
-- book 书目（一种书一条记录，复本数量在 book_stock）
-- total_copies 是【反范式冗余字段】：仅用于列表展示，权威值在 book_stock.total_copies，
-- 由库存变更事务内同步。冗余的原因：书目列表页 QPS 极高，避免与库存表关联查询。
-- 一致性代价与取舍见 docs/09-多种解决方案对比手册.md（P7 节）。
-- -----------------------------------------------------------------------------
create table book (
    id              bigint auto_increment primary key,
    isbn            varchar(20)   not null,
    title           varchar(200)  not null,
    author          varchar(100),
    publisher       varchar(100),
    category_id     bigint,
    publish_year    int,
    price           decimal(10,2) not null default 0.00,
    tags            varchar(255),                  -- 逗号分隔关键词，参与检索加权
    summary         varchar(1000),                 -- 内容简介，参与 Agent 推荐
    shelf_location  varchar(64),                   -- 馆藏位置，如 "三楼东区 TP311/1287"
    total_copies    int           not null default 0, -- 冗余展示字段，见上方说明
    created_at      datetime      not null,
    updated_at      datetime      not null
);

-- -----------------------------------------------------------------------------
-- book_stock 库存（与 book 一对一）
-- 这是全系统库存的唯一事实源；available_copies 永不允许为负。
-- version 用于乐观锁策略（VersionRetryStockStrategy）。
-- -----------------------------------------------------------------------------
create table book_stock (
    book_id           bigint primary key,
    total_copies      int      not null default 0,
    available_copies  int      not null default 0,
    locked_copies     int      not null default 0, -- 已预扣未落库（Redis 预扣策略用）
    version           int      not null default 0,
    updated_at        datetime not null
);

-- -----------------------------------------------------------------------------
-- borrow_record 借阅流水
-- -----------------------------------------------------------------------------
create table borrow_record (
    id           bigint auto_increment primary key,
    reader_id    bigint      not null,
    book_id      bigint      not null,
    borrow_at    datetime    not null,
    due_at       datetime    not null,
    return_at    datetime,
    renew_count  int         not null default 0,
    status       varchar(20) not null,             -- BORROWED / RETURNED / OVERDUE / LOST
    fine         decimal(10,2) not null default 0.00,
    created_at   datetime    not null,
    updated_at   datetime    not null
);

-- -----------------------------------------------------------------------------
-- reservation 预约队列
-- queue_no 为同一 book_id 下的排队序号，用于 FIFO 出队。
-- -----------------------------------------------------------------------------
create table reservation (
    id          bigint auto_increment primary key,
    reader_id   bigint      not null,
    book_id     bigint      not null,
    queue_no    int         not null,
    status      varchar(20) not null,              -- WAITING / READY / FULFILLED / EXPIRED / CANCELLED
    ready_at    datetime,
    expire_at   datetime,
    created_at  datetime    not null,
    updated_at  datetime    not null
);

-- -----------------------------------------------------------------------------
-- stock_ledger 库存流水（审计与对账的唯一依据）
-- 不变式：对同一 book_id，sum(delta) 等于 available_copies 的累计变化量。
-- biz_no 全局唯一，保证同一次业务操作不会重复写流水。
-- -----------------------------------------------------------------------------
create table stock_ledger (
    id          bigint auto_increment primary key,
    book_id     bigint      not null,
    op_type     varchar(20) not null,              -- DEDUCT / RELEASE / ROLLBACK / INIT
    delta       int         not null,              -- 负数表示扣减，正数表示归还/回补
    before_qty  int         not null,
    after_qty   int         not null,
    biz_no      varchar(64) not null,
    strategy    varchar(32),                       -- 产生该流水的扣减策略，便于压测对比
    trace_id    varchar(64),
    created_at  datetime    not null
);

-- -----------------------------------------------------------------------------
-- idempotent_record 幂等记录
-- 借书/续借/预约等写操作必须带幂等键；重复请求直接返回首次结果。
-- -----------------------------------------------------------------------------
create table idempotent_record (
    id          bigint auto_increment primary key,
    idem_key    varchar(128) not null,
    biz_type    varchar(32)  not null,
    result_json varchar(2000),
    created_at  datetime     not null
);

-- -----------------------------------------------------------------------------
-- rule_config 借阅规则配置
-- 目标：改规则不发版。reader_type 为 '*' 表示对所有读者类型生效。
-- -----------------------------------------------------------------------------
create table rule_config (
    id          bigint auto_increment primary key,
    rule_key    varchar(64)  not null,
    reader_type varchar(20)  not null default '*',
    rule_value  varchar(255) not null,
    remark      varchar(255),
    version     int          not null default 0,
    updated_at  datetime     not null
);

-- =============================================================================
-- 索引
-- =============================================================================

-- reader
create unique index uk_reader_card_no on reader (card_no);

-- category
create unique index uk_category_code on category (code);

-- book
create unique index uk_book_isbn on book (isbn);
create index idx_book_category on book (category_id, publish_year);
create index idx_book_publisher on book (publisher);
create index idx_book_title on book (title);

-- book_stock：available_copies 索引服务"仅看可借"筛选
create index idx_stock_available on book_stock (available_copies);

-- borrow_record
create index idx_borrow_reader_status on borrow_record (reader_id, status);
create index idx_borrow_book_status on borrow_record (book_id, status);
create index idx_borrow_due_status on borrow_record (due_at, status);

-- reservation
create index idx_reservation_book_status on reservation (book_id, status, queue_no);
create index idx_reservation_reader on reservation (reader_id, status);

-- stock_ledger
create unique index uk_ledger_biz_no on stock_ledger (biz_no);
create index idx_ledger_book_time on stock_ledger (book_id, created_at);

-- idempotent_record
create unique index uk_idem_key on idempotent_record (idem_key);

-- rule_config
create unique index uk_rule_key_type on rule_config (rule_key, reader_type);
