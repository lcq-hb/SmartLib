# SmartLib · 高校图书馆智能借阅与推荐平台

> Java 后端（业务主链路）+ Python Agent 服务（智能助手）的双服务项目。
> 面向 **Agent 应用开发 / Java 后端实习** 岗位，完整覆盖需求 → 设计 → 编码 → 测试 → 部署 → 运维迭代的软件生命周期。

[![Java](https://img.shields.io/badge/Java-21-007396)](https://openjdk.org/)
[![Spring Boot](https://img.shields.io/badge/Spring%20Boot-4.1.1-6DB33F)](https://spring.io/projects/spring-boot)
[![MyBatis](https://img.shields.io/badge/MyBatis-4.1.0-red)](https://mybatis.org/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688)](https://fastapi.tiangolo.com/)

---

## 1. 这是什么

某高校图书馆（3.2 万师生、120 万册馆藏、年借阅 96 万次）现有系统是 2013 年的单体 Java 应用 + 存储过程，存在六个真实痛点：

| 编号 | 痛点 | 后果 |
|---|---|---|
| P1 | **热门书超卖**：多窗口并发借同一本书，库存被扣成负数 | 系统显示可借但书已被借走，2024 年投诉 37 起 |
| P2 | **检索慢**：`like '%kw%'` 全表扫描 120 万行 | 平均 2.8 秒，峰值超时 8 秒，无分词/拼音/相关度 |
| P3 | **单点故障**：借阅服务单实例 | 一次故障导致全馆借还停摆 47 分钟 |
| P4 | **服务台压力**：读者找不到书、不清楚规则 | 日均咨询 260 人次，68% 是重复问题 |
| P5 | **人工催还**：逾期靠馆员手工导 Excel 打电话 | 人力成本高、触达率低 |
| P6 | **规则硬编码**：册数/期限散落在存储过程与常量里 | 改一次规则要发版，平均 3 天生效 |

本项目用 **Java 后端 + Python Agent** 解决这六个问题，并且每个问题都给出**多种候选方案**、选型理由、落地代码与实测数据。

---

## 2. 六个痛点 → 多方案落地（项目核心）

> 每个问题都按「现象 → 候选方案 → 对比 → 选型 → 代码 → 数据」展开，完整版见 [`docs/09-多种解决方案对比手册.md`](docs/09-多种解决方案对比手册.md)。

| 问题 | 候选方案 | 默认选型 | 落地位置 |
|---|---|---|---|
| **P1 库存超卖** | ①单机锁 ②DB 条件更新 ③DB 悲观锁 ④乐观锁 version 重试 ⑤Redis+Lua 原子预扣 ⑥MQ 串行化 | ② DB 条件更新（正确性优先、组件最少） | `service/stock/` 六种实现，同一 SPI，配置切换 |
| **P2 检索慢** | ①`like %%` 基线 ②前缀索引 ③**自研倒排索引** ④MySQL ngram 全文索引 ⑤Elasticsearch | ③ 自研倒排（零外部依赖、可解释） | `service/search/` 五个引擎实现同一 `SearchEngine` 接口 |
| **P3 单点故障** | ①无状态化 ②多实例+Nginx ③Nacos ④令牌桶限流 ⑤熔断降级 ⑥幂等+优雅停机+探针 | 全部落地（分层防御） | `support/`、`web/`、`deploy/` |
| **P4 服务台压力** | ①纯 Prompt ②Function Calling ③RAG ④Plan-Execute ⑤规则路由+LLM 兜底 | ⑤ 混合模式（高频走规则，长尾走 LLM） | `agent-service/app/agent/` |
| **P5 人工催还** | ①定时扫描落库罚金 ②实时计算 ③消息推送 | ① 定时扫描（查询接口保持纯读） | `service/ScheduledTasks.java` |
| **P6 规则硬编码** | ①常量 ②配置文件 ③**数据库规则表** ④规则引擎 | ③④ `rule_config` 表 + `RuleEngine` | `service/RuleEngine.java`，改规则不发版 |

**横切优化**：缓存三件套（布隆防穿透 / 单飞防击穿 / TTL 抖动防雪崩）、三级缓存（Caffeine → Redis → DB）、深分页优化、幂等表、全链路 `trace_id` 贯穿 Java 与 Python。

---

## 3. 架构

```
[读者端]            [馆员后台]         [运维台]
    │                   │                │
    └──────────┬────────┴────────────────┘
               ▼
        [Nginx / Ingress]   限流 · 负载 · SSE 透传
               │
   ┌───────────┴────────────────────────────┐
   │   smartlib-backend (Java, 无状态 ×N)    │
   │  web → service → mapper → MySQL/Redis  │
   │  stock(6策略 SPI) · search(5引擎 SPI)   │
   │  cache(布隆/多级/单飞) · support(限流/熔断) │
   └───────────┬────────────────────────────┘
               ▲ HTTP + HMAC 签名（/internal/agent/**）
               │
   ┌───────────┴────────────────────────────┐
   │   smartlib-agent (Python FastAPI ×N)   │
   │  意图路由 → ReAct 循环 → 工具执行 → 护栏  │
   │  RAG(BM25+向量) · 记忆 · 评测           │
   └────────────────────────────────────────┘
```

详细设计见 [`docs/02-概要设计.md`](docs/02-概要设计.md)、[`docs/03-详细设计.md`](docs/03-详细设计.md)。

---

## 4. 快速开始（零外部依赖）

后端用 H2 内存库、Agent 用 MockLLM，**不需要 MySQL / Redis / Elasticsearch / 任何 API Key**。

### 4.1 后端

```powershell
# 方式一：用项目自带脚本（推荐，见下方"为什么有 mvn.ps1"）
powershell -ExecutionPolicy Bypass -File scripts/mvn.ps1 -f backend/pom.xml spring-boot:run

# 方式二：标准 Maven
cd backend
mvn spring-boot:run
```

启动后：
- Swagger UI：<http://localhost:8080/swagger-ui.html>
- 健康检查：<http://localhost:8080/actuator/health>
- H2 控制台：<http://localhost:8080/h2-console>（JDBC URL `jdbc:h2:mem:smartlib`，用户 `sa`，密码空）

试一下：

```bash
# 检索（自研倒排索引）
curl "http://localhost:8080/api/books?query=计算机网络&size=3"

# 借书（必须带幂等键）
curl -X POST http://localhost:8080/api/borrows \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-$(date +%s)" \
  -d '{"readerId":1,"bookId":3}'

# 用同一个 Idempotency-Key 再发一次：不会重复扣库存，返回 idempotentReplay=true
```

### 4.2 Agent 服务

```powershell
cd agent-service
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

```bash
# 同步对话（便于脚本调用与测试）
curl -X POST http://localhost:8000/chat/sync \
  -H "Content-Type: application/json" \
  -d '{"reader_id":1,"message":"帮我借一本计算机网络，顺便推荐两本同类的"}'

# 流式对话（SSE）
curl -N -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"reader_id":1,"message":"本科生能借几本书？"}'
```

> Agent 默认 `LLM_PROVIDER=mock`，用确定性规则模拟模型行为，**没有 API Key 也能完整演示与跑测试**。
> 接真实模型只需设置 `LLM_PROVIDER=openai_compat`、`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。

### 4.3 一键启动（后端 + Agent）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1
```

### 4.4 演示控制台（面试演示用）

两个服务都起来之后，直接打开：

**<http://127.0.0.1:8080/demo.html>**

这是一个单文件、零外部依赖的演示控制台，把五个关键点做成按钮：

| 卡片 | 演示什么 | 看得见的证据 |
|---|---|---|
| ▶ 一键演示 | 按顺序跑完全部 5 步 | 每步一行的进度日志 |
| 1 书目检索 | 中文关键词 与 **拼音首字母 `jsjwl`** | 命中条数、耗时、traceId、结果表 |
| 2 借书幂等 | 用**同一个 Idempotency-Key** 连发两次 | 第二次 `idempotentReplay: true` 且 `borrowId` 相同 |
| 3 对账与策略 | `sum(stock_ledger.delta)` 守恒；六种策略**不重启切换** | `consistent: true`、`violationCount: 0` |
| 4 我的借阅 | 一次 join 取回书名与应还日期 | 真实应还日期与逾期天数 |
| 5 Agent 对话 | 工具调用 / RAG / 护栏 | `tool_calls`（工具名+真实参数+结果摘要）、意图置信度、护栏拦截、是否有工具证据 |
| 6 内部接口签名 | 现场用 WebCrypto 算 HMAC-SHA256 | 完整签名规范串与签名值 |

几个刻意的设计：

- **页面放在后端的静态资源里**（`backend/src/main/resources/static/demo.html`），
  因此页面地址与 `/api/**` 同源，**完全不需要 CORS**；只有调用 Agent（8000）是跨域的，
  为此给 Agent 加了一段**白名单** CORS（默认只放本机，见 `agent-service/app/config.py` 的 `cors_origins`）。
- **不引任何 CDN 或框架**：演示环境可能断网，外部依赖都是失败点。
- **每步都显示 HTTP 状态码 / 耗时 / trace_id 与原始 JSON**，演示时不需要打开浏览器开发者工具。
- 第 2 步提供"自动选一本可借的"按钮：它会查该读者当前在借、再查可借书目，
  自动挑一本**没被该读者持有**的书 —— 避免演示时撞上"同一读者不能重复借同一本书"的规则。
- 页面里那段 HMAC 签名与 Python 客户端、Java 服务端**三方算法一致**，已用固定向量交叉验证过。

> 提示：H2 是内存库，**重启后端即恢复全部演示数据**。
> 反复演示导致库存借空时，重启后端或换一本书即可。

---

## 5. 测试与基准

```powershell
# 后端全部测试
powershell -ExecutionPolicy Bypass -File scripts/mvn.ps1 -f backend/pom.xml test

# Agent 测试与评测
cd agent-service
.venv\Scripts\python -m pytest tests -q
.venv\Scripts\python -m app.eval.run_eval --provider mock

# 基准测试（六种库存策略 + 各检索引擎对比，输出 Markdown 表格）
powershell -ExecutionPolicy Bypass -File scripts/bench/run_all.ps1
```

### 已实测结果

> 以下数字来自本机真实运行的测试与基准脚本（`backend/src/test/java/com/smartlib/`）。
> 完整报告见 [`docs/06-测试计划与报告.md`](docs/06-测试计划与报告.md)。

<!-- MEASURED_RESULTS_START -->

**后端测试：`Tests run: 41, Failures: 0, Errors: 0, Skipped: 0` — BUILD SUCCESS**

环境：Windows / JDK 25（字节码 release 21）/ H2 2.5.252 内存库（`MODE=MySQL`）/ 单 JVM，**未做 JVM 调优**，因此延迟绝对值仅适合方案间横向对比。

#### ① 六种库存扣减策略 · 200 并发抢 10 册（`StockConcurrencyTest`）

| 策略 | 成功扣减 | 失败 | 最终库存 | 流水之和 | P50 | P99 | 超卖 |
|---|---|---|---|---|---|---|---|
| `optimistic-db`（默认） | 10 | 190 | 0 | −10 | 20ms | 27ms | **0** |
| `pessimistic-db` | 10 | 190 | 0 | −10 | 18ms | 27ms | **0** |
| `version-retry` | 10 | 190 | 0 | −10 | 12ms | 19ms | **0** |
| `local-lock` | 10 | 190 | 0 | −10 | 16ms | 27ms | **0** |
| `redis-lua` | 10 | 190 | 0 | −10 | 322ms | 351ms | **0** |
| `mq-serial` | 10 | 190 | 0 | −10 | 24ms | 28ms | **0** |

六种策略全部满足四条不变式：成功次数 ≤ 10、可用库存 ≥ 0 且 = 10 − 成功次数、流水之和 = −10、对账 SQL 无异常。

关于 `redis-lua` 的 322/351ms：**本机没有部署 Redis**，每个请求都要先付一次连接失败/超时的代价才降级到数据库。这是降级路径的真实代价，不是 Redis 方案本身的性能。加短路保护后该场景 P99 从 **621ms 降到 351ms**（200 个请求同时到达，短路只对后续请求生效；生产持续流量下绝大多数请求可免于这个代价）。

原始产物：[`scripts/bench/results/stock-strategy-bench.md`](scripts/bench/results/stock-strategy-bench.md)（由测试自动生成）

#### ② 检索引擎对比 · 20,000 条书目 / 15 查询 × 10 轮 / TopK=10（`SearchEngineBenchmarkTest`）

| 引擎 | P50 | P95 | P99 | Max | QPS | 召回率@10 |
|---|---|---|---|---|---|---|
| `like`（Q1 基线） | 11ms | 14ms | 21ms | 22ms | 54.6 | 83.33% |
| `inverted-index`（Q3 自研倒排） | 1ms | 4ms | 5ms | 5ms | 420.5 | 98.67% |

**P99 提升约 4.2 倍，QPS 提升约 7.7 倍。**

召回率口径必须说明清楚，否则会误读：标注答案是「书名包含查询串」。`like` 的 WHERE 还包含 `tags`/`author`/`publisher` 匹配，所以它的 Top10 会混入"标签命中但书名不含"的书，只算 83.33%；倒排索引给了书名 3.0 权重并对"书名包含完整查询串"加 8 分短语加成，Top10 几乎全是书名命中，因此 98.67%。**这是字段加权与短语加成带来的相关度质量差异，不是"倒排索引召回率天然更高"。**

本测试直接调用引擎、绕过缓存，测的是真实计算开销；20,000 条是为让 CI 快速跑完的规模，生产是 120 万条，本表用于引擎间相对对比。

#### ③ 测试、压测与联调中发现并修复的 10 个真实缺陷

| # | 现象 | 根因 | 影响 |
|---|---|---|---|
| 1 | 还书接口 100% 失败 | `markReturned` 是 UPDATE 却被标为 `@Select` → 返回 null → 基本类型 `int` 抛 `BindingException` | **还书功能完全不可用** |
| 2 | 应用启动直接失败 | `RuleEngine.refresh()` 放了 null 值后调用 `Map.copyOf` → NPE | 服务无法启动 |
| 3 | 应用启动直接失败 | `@Scheduled(cron=…)` 同时带 `initialDelay`，Spring 明确不支持该组合 | 服务无法启动。**测试 profile 关掉了定时任务，所以测试全绿也照样炸** —— 靠真实启动才发现 |
| 4 | 检索 SQL 报错 | MyBatis `#{q.offset}` 只认标准 getter `getOffset()`，实现写的是 `offset()` | 检索不可用 |
| 5 | **用拼音首字母搜不到中文书名** | 索引侧存的是 `py:jsjwl`，查询侧只生成裸词项 `jsjwl`，两边永远对不上 | 一个写在文档里的功能实际上完全不可用 |
| 6 | **报成功 8 次、实际扣减 10 次** | 同步等待 MQ 结果超时后直接报失败，但 `Future.cancel(false)` 对已开始执行的任务无效，扣减已经发生 | **库存扣了但用户以为没借到** |
| 7 | 200 并发下连接池死锁 | 调用方在事务中等待工作线程结果并占住连接，工作线程拿不到连接 | 大量请求超时 |
| 8 | Redis 挂掉后借阅 P99 从 27ms 恶化到 621ms | 降级路径缺少短路，每个请求都付一次连接超时 | 缓存故障演变成业务故障 |
| 9 | **借出 8 册后详情接口仍显示可借 8** | 详情缓存里含 `availableCopies`，但库存变化时**没有任何代码去失效它**（当时唯一的监听器只更新倒排索引） | 读者看到偏大的可借数，白跑到馆 |
| 10 | 本地 24 书目、容器里只有 14 书目 | 应用侧与部署侧的种子 SQL 各写一份且内容不一致 | 演示环境与本地行为不一致，且与数据库设计文档矛盾 |

第 5、9、10 条特别值得讲：**它们都不是"代码报错"，而是"测试全绿但功能是坏的"**。
第 5 条靠端到端检索用例发现，第 9 条靠真实压测发现，第 10 条靠对比两侧 SQL 发现 ——
这也是本项目除单元测试外，坚持做**真实启动**与**端到端联调**的原因。
第 10 条的根治办法是把种子数据写成 H2 与 MySQL 的**可移植子集**（两边共用同一份文件），
并用 `scripts/tools/check_sql_sync.py` 在 CI 中强制校验，杜绝再次漂移。

详见 [`docs/06-测试计划与报告.md`](docs/06-测试计划与报告.md) 的缺陷记录表与 [`docs/09-多种解决方案对比手册.md`](docs/09-多种解决方案对比手册.md) 的踩坑记录。

#### ③-b Java ↔ Python 端到端联调（已实测）

Java 后端（8080）与 Agent 服务（8000）同时以真实进程运行，用中文自然语言提问，
Agent 通过 **HMAC-SHA256 签名**调用后端内部接口，答案必须来自真实数据：

| 提问 | 意图 | 实测结果 |
|---|---|---|
| 本科生最多能借几本书？借期多久？ | `policy_qa` | 「本科生类型读者最多可同时借阅 10 册。借期为 30 天，可续借 2 次…（依据：rule_config）」 |
| 《计算机网络》现在还有库存吗？在哪个架位？ | `get_stock` | 「《计算机网络：自顶向下方法》馆藏 6 册，当前可借 5 册，索书位置：三楼东区 TP393/771」 |
| 帮我借《算法导论》 | `borrow_book` | 正确要求二次确认，**未直接执行写操作** |
| 我是读者 2002，帮我借《活着》 | `borrow_book` | 「为了账号安全，我只能帮您本人办理借阅相关业务，无法操作其他读者的账号。」 |
| 推荐几本计算机方面的书 | `recommend` | 返回真实书目与可解释理由 |
| 我的借阅记录里有哪些书快到期了？ | `my_loans` | 返回真实应还日期 |

联调过程中另外抓出两个**只在真实链路里才暴露**的缺陷：
① 中文查询参数的签名必须对「**百分号编码后**的请求目标」计算 —— 用 ASCII 参数联调完全看不出问题；
② Agent 不能采信模型给出的 `book_id`，必须以 `search_books` 的返回为准（对真实大模型同样必要）。

#### ③-c 基准脚本的第二组独立实测（`scripts/bench/run_all.ps1`）

| 指标 | like（Q1 基线） | inverted-index（Q3 自研倒排） |
|---|---|---|
| 平均召回率（42 条人工标注查询） | 64.29% | **100.00%** |
| Top1 命中率 | 64.29% | **100.00%** |
| 检索 QPS（4 线程，小数据集） | 4046 | 2648 |

六种库存策略在"每轮独立库存 + 干净读者池"的前提下各 **40/40 成功、0% 错误率**，
逐轮明细（借前 8 → 借后 0 → 归还后 8）与对账结论见
[`scripts/bench/results/summary.md`](scripts/bench/results/summary.md)。

> 口径提醒：该脚本用纯标准库客户端、每请求新建连接，因此 QPS 是**下界**；
> 小数据集下 like 的 QPS 甚至高于倒排（无网络与连接开销差异时，全表扫描 24 行当然快），
> 所以"like 全表扫描慢"的结论必须在大数据量下才成立 —— 见 ④ 中的未验证项。

#### ④ 仍未实测的项目（如实标注，未回填）

Elasticsearch 引擎（本机未部署 ES）· Redis+Lua 的**真实成功路径**（本机无 Redis，实测的是降级路径）· MySQL 8（测试跑在 H2 `MODE=MySQL`）· Docker Compose / K8s / Nginx（未实际启动）· Agent 接入真实大模型（MockLLM 已测）· 120 万条生产规模 · Agent 各项准确率指标。

<!-- MEASURED_RESULTS_END -->

---

## 6. 目录结构

```
.
├── backend/                        # Java 后端（Spring Boot 4.1.1 + MyBatis）
│   ├── src/main/java/com/smartlib/
│   │   ├── common/                 # 统一响应、错误码、全局异常、traceId
│   │   ├── config/                 # 缓存、OpenAPI、MyBatis 装配
│   │   ├── domain/                 # 实体（代码生成）、枚举、DTO、读模型
│   │   ├── mapper/                 # MyBatis Mapper（注解 + XML 混用）
│   │   ├── cache/                  # 布隆过滤器、多级缓存、单飞护栏
│   │   ├── support/                # 令牌桶限流、熔断器、降级兜底
│   │   ├── service/                # 业务服务
│   │   │   ├── stock/              # 六种库存扣减策略（SPI）
│   │   │   └── search/             # 五种检索引擎（SPI）+ 分词 + 拼音
│   │   └── web/                    # 控制器与过滤器（限流、HMAC 签名）
│   └── src/main/resources/
│       ├── db/schema.sql           # 可移植 DDL（MySQL 8 与 H2 MODE=MySQL 通用子集）
│       ├── db/data.sql             # 演示数据
│       ├── mapper/BookMapper.xml   # 动态检索 SQL
│       └── lua/stock_deduct.lua    # Redis 原子预扣脚本
├── agent-service/                  # Python Agent 服务（FastAPI）
│   └── app/{agent,llm,tools,rag,memory,client,api,eval}/
├── deploy/                         # 部署设施（Compose / K8s / Nginx / Dockerfile）
├── scripts/                        # 工具与基准脚本
│   ├── mvn.ps1                     # 项目专用 Maven 启动器（见下方说明）
│   ├── tools/gen_entities.py       # 实体类代码生成器
│   └── bench/                      # 压测与对比基准
└── docs/                           # 完整生命周期文档（见下）
```

---

## 7. 文档（完整软件生命周期）

| 阶段 | 文档 |
|---|---|
| 立项 | [项目蓝图（唯一技术契约）](docs/00-项目蓝图.md) |
| 需求 | [需求分析](docs/01-需求分析.md) · [需求评审纪要](docs/reviews/01-需求评审纪要.md) |
| 设计 | [概要设计](docs/02-概要设计.md) · [详细设计](docs/03-详细设计.md) · [数据库设计](docs/04-数据库设计.md) · [ADR](docs/11-架构决策记录ADR.md) · [概设评审](docs/reviews/02-概要设计评审纪要.md) · [详设评审](docs/reviews/03-详细设计评审纪要.md) |
| 编码 | [编码规范与工程实践](docs/05-编码规范与工程实践.md) |
| 测试 | [测试计划与报告](docs/06-测试计划与报告.md) · [测试结论评审](docs/reviews/04-测试结论评审纪要.md) |
| 部署 | [部署上线手册](docs/07-部署上线手册.md) · [上线评审](docs/reviews/05-上线评审纪要.md) |
| 运维迭代 | [运维监控与迭代规划](docs/08-运维监控与迭代规划.md) · [项目复盘与维护手册](docs/12-项目复盘与维护手册.md) · [故障复盘](docs/reviews/06-故障复盘纪要.md) |
| **方案对比** | [多种解决方案对比手册（核心）](docs/09-多种解决方案对比手册.md) |
| 求职 | [简历话术与面试问答](docs/10-简历话术与面试问答.md) |

---

## 8. 本项目的几个诚实说明

技术文档里最容易失真的部分是"数据"。本项目对此的处理原则是：**能用代码测出来的就实测，测不出来的就明确标注为目标值或未验证**。

- **已实测**：库存六策略的并发正确性（200 并发抢 10 册）、检索双引擎延迟与召回对比、幂等重放、HMAC 签名、限流与熔断状态机、对账不变式。
- **未实测（已标注）**：Elasticsearch 引擎（本机未部署 ES，只有代码与接口契约）、Redis+Lua 预扣的**真实 Redis 路径**（本机无 Redis，实测的是"Redis 不可用自动降级且仍不超卖"这条更关键的路径）、生产的 120 万级数据规模（测试用 2 万条，脚本支持更大规模）。
- **Mock 与真实的边界**：Agent 默认使用 MockLLM，因此 Agent 的准确率指标是"在确定性模拟模型下的工程链路正确性"，**不等于接入真实大模型后的效果**；接真实模型需重新跑 `run_eval.py`。
- **文档中的历史数据**（2.8 秒检索、37 起投诉、47 分钟停摆）来自蓝图 §1.2 设定的现状基线，属于项目情境设定，不是本项目生产环境的实测值。

---

## 9. 为什么有 `scripts/mvn.ps1`

本项目的开发路径包含中文字符（`...\简历项目\...`）。官方的 `mvn.cmd` 是批处理脚本，参数会经过 `cmd.exe` 的代码页转换，导致 `-Dmaven.repo.local` 等属性被丢弃、Maven 回退到默认的 `~/.m2` 仓库。

`scripts/mvn.ps1` 直接调用 Maven 的 classworlds launcher，参数由 PowerShell 以 Unicode 传给 `java.exe`，中文路径完全安全。它同时把本地仓库固定在 `.tools/m2repo`，避免污染全局环境。

如果你把项目放在纯 ASCII 路径下，直接用 `mvn` 也没有任何问题。
