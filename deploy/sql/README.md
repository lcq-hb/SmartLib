# deploy/sql 说明

本目录是**部署用 SQL**，供 MySQL 容器初始化（`docker-entrypoint-initdb.d`）与手工上线使用。

## 文件与权威来源

| 文件 | 用途 | 权威来源（改这里！） |
|---|---|---|
| `01-schema.sql` | 建表 + 建索引 | `backend/src/main/resources/db/schema.sql` |
| `02-seed.sql` | 初始化数据（分类/读者/书目/库存/规则/演示借阅） | `backend/src/main/resources/db/data.sql` |
| `mysql-production-extras.sql` | MySQL 生产专用优化：ngram 全文索引、`borrow_record` 分区、最小权限账号、慢查询参数 | 无副本，本目录独有 |

> **为什么有两份**：应用启动时要自己建表灌数（H2 内存库、`MODE=MySQL`），
> 而 MySQL 容器初始化只认 `docker-entrypoint-initdb.d` 下的 `.sql` 文件，
> 两者无法共用同一个路径。因此采用"**应用侧为权威、部署侧为副本**"的策略。

## 硬规则：不允许漂移

两份 DDL/DML 必须**逐条一致**（只允许部署侧多出纯 MySQL 环境语句，如 `SET NAMES`、`ALTER DATABASE`）。

这条规则不是洁癖，而是踩过坑：

> 曾经 `deploy/sql/02-seed.sql` 只有 14 本书、11 名读者，
> 而应用侧的 `data.sql` 有 24 本书、12 名读者。
> 结果是**本地演示一切正常，Docker 起来少了一半书目**，
> 且与 `docs/04-数据库设计.md` 记载的"24 书目 / 12 读者"对不上。
> 这类不一致极难在评审时靠肉眼发现。

因此：

```bash
# 提交前自检（CI 的 guards job 也会跑，不一致直接红）
python scripts/tools/check_sql_sync.py --verbose
```

## 语法约束：只写"可移植子集"

两份 SQL 都只使用 **H2 `MODE=MySQL` 与 MySQL 8.0 都支持**的语法：

| 场景 | 允许 | 禁止 | 原因 |
|---|---|---|---|
| 当前时间 | `NOW()` | `current_timestamp`（H2 大小写/函数差异） | 统一写法 |
| 时间偏移 | `TIMESTAMPADD(DAY, n, NOW())` | `DATE_ADD(x, INTERVAL n DAY)`（**H2 MySQL 模式实测不支持**）、H2 的 `dateadd` | 实测验证过的交集 |
| 字符串拼接 | `CONCAT(a, b)` | `a \|\| b` | MySQL 默认把 `\|\|` 当逻辑或，会静默算错 |
| 建表 | 独立 `CREATE INDEX` | `ENGINE=` / 行内 `COMMENT` / 行内 `UNIQUE KEY` | H2 不认 MySQL 私有语法 |
| 关联插入 | `INSERT ... SELECT ... JOIN` | 写死自增 id | 两个库的自增序列行为不同 |

`ALTER DATABASE ... CHARACTER SET` 这类纯 MySQL 语句只允许出现在 `01-schema.sql`（部署侧），
校验脚本会忽略它们。

## MySQL 生产专用内容

`mysql-production-extras.sql` 里的优化**不在**可移植子集范围内，因此不参与漂移校验，只在 MySQL 上执行：

- `FULLTEXT ... WITH PARSER ngram` 全文索引（对应检索引擎方案 Q4）
- `borrow_record` 按年 RANGE 分区（年增 96 万行的归档策略）
- 应用/只读/DDL 三类账号的最小权限 `GRANT`
- 慢查询与连接相关参数建议

## 常用命令

```bash
# 本地零依赖起后端（用 H2，自动执行 backend 侧的 schema + data）
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1 -SkipAgent

# 用 Docker Compose 起 MySQL 环境（执行本目录的 01/02）
docker compose -f deploy/docker-compose.yml up -d mysql

# 手工在已有 MySQL 上初始化
mysql -h 127.0.0.1 -u smartlib_ddl -p smartlib < deploy/sql/01-schema.sql
mysql -h 127.0.0.1 -u smartlib_ddl -p smartlib < deploy/sql/02-seed.sql
mysql -h 127.0.0.1 -u root -p smartlib < deploy/sql/mysql-production-extras.sql

# 校验两份 SQL 是否一致
python scripts/tools/check_sql_sync.py --verbose
```
