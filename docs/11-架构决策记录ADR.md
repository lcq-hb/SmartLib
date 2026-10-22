# SmartLib 架构决策记录（ADR）

> 项目代号：**SmartLib**｜文档版本：v1.0.0｜阶段：M2 设计阶段门
> 上游契约：[`docs/00-项目蓝图.md`](./00-项目蓝图.md)（技术栈版本、类名、接口路径、表名以其为准）
> 关联文档：[`docs/02-概要设计.md`](./02-概要设计.md)、[`docs/03-详细设计.md`](./03-详细设计.md)、[`docs/04-数据库设计.md`](./04-数据库设计.md)

## 说明

- **ADR 是什么**：一条不可变、可追溯的架构决策记录。记录了"当时面对什么问题、选了什么、为什么不选别的、代价是什么"。
- **状态语义**：`已接受` = 当前生效，代码按此实现；`已废弃` = 被后续 ADR 取代；`待定` = 尚未定案（本文档无）。
- **修改规则**：ADR 一旦"已接受"即**不再修改其内容**。决策变更必须新增一条 ADR，并在旧条目上标注"被 ADR-0xx 取代"。
- **索引**：

| ADR | 主题 | 状态 |
|---|---|---|
| [ADR-001](#adr-001) | 为何选 Spring Boot 4.1.1 + Java 21 | 已接受 |
| [ADR-002](#adr-002) | 为何用 MyBatis 而非 JPA | 已接受 |
| [ADR-003](#adr-003) | 为何不使用 Lombok | 已接受 |
| [ADR-004](#adr-004) | 库存扣减默认选 DB 条件更新（S2） | 已接受 |
| [ADR-005](#adr-005) | 为何自研限流熔断而非直接上 Sentinel | 已接受 |
| [ADR-006](#adr-006) | 检索引擎默认自研倒排，ES 为可选 | 已接受 |
| [ADR-007](#adr-007) | `book` 与 `book_stock` 分表 | 已接受 |
| [ADR-008](#adr-008) | H2 + MySQL 双环境（`MODE=MySQL`） | 已接受 |
| [ADR-009](#adr-009) | 幂等落库表而非纯 Redis | 已接受 |
| [ADR-010](#adr-010) | Agent 用自研 ReAct 循环而非 LangChain | 已接受 |
| [ADR-011](#adr-011) | Agent 默认 MockLLM 可离线运行 | 已接受 |
| [ADR-012](#adr-012) | 库存流水 `stock_ledger` 强审计 | 已接受 |

> **性能数据声明**：本文档中所有耗时/QPS 数字，除标注"现状基线"外均为**设计目标值**或**待实测回填项**；项目尚未完成压测，禁止编造实测数据。实测值统一回填 [`docs/06-测试计划与报告.md`](./06-测试计划与报告.md)。

---

## ADR-001

### 为何选择 Spring Boot 4.1.1 与 Java 21

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审（本人 + 模拟评审） |

**背景**
项目需要一套能在本机与 CI 上稳定复现、且能作为"现代化 Java 后端能力证明"的技术底座。候选底座有三类：Spring Boot 3.x（生态最成熟）、Spring Boot 4.1.x（最新一代）、以及非 Spring 的轻量框架（如 Quarkus/Micronaut）。同时需要选定字节码基线（Java 17 / 21 / 25）。

**决策**
采用 **Spring Boot 4.1.1** 作为应用框架，**Java 21** 作为编译与字节码基线（本机运行在 Java 25 上，向下兼容）。版本已在 `spike/` 模块实际验证可用。

**理由**
1. **已验证而非假设**：`spike/pom.xml` 以 `spring-boot-starter-parent:4.1.1` + `java.version=21` + `mybatis-spring-boot-starter:4.1.0` 的组合跑通了「H2 `MODE=MySQL` + MyBatis + REST + 集成测试」，风险已被前置消解。
2. **Java 21 是 LTS 且语法收益实在**：`record`（本项目全部 DTO 采用）、模式匹配 `switch`（错误码与枚举分发）、虚拟线程（可选的 DB 调用优化路径）、文本块（MyBatis SQL 说明与测试断言可读性）。Java 25 虽为 LTS 但生态适配尚新，只作为运行环境而非字节码基线。
3. **Spring Boot 4.x 是当前主流代际**：选择 4.1.1 而非 3.x，使项目的依赖治理（如 Jakarta EE 命名空间、新的自动配置机制）与技术选型中的"当前主流"对齐；同时它带来的兼容风险通过 spike 已被验证覆盖。
4. **Actuator/Micrometer/Prometheus 开箱即用**，直接满足蓝图 §7 的可观测性目标，无需额外造轮子。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| Spring Boot 3.x | 生态更稳，但项目定位是"展示当前工程能力"，且已验证 4.1.1 可用，没有降级必要；降级会牺牲"跟进新版本"的叙事价值 |
| Java 17 字节码 | LTS 但缺少 `record` 之外的模式匹配与虚拟线程等本项目会用到的能力，无收益 |
| Java 25 字节码 | 本机可跑，但第三方字节码处理工具（测试覆盖率、部分插件）适配尚新，CI 风险不划算 |
| Quarkus / Micronaut | 启动快、内存省，但岗位 JD 与团队技术栈普遍是 Spring；生态（MyBatis/Actuator/springdoc）契合度低，学习成果转移性差 |

**正面后果**
- 技术栈现代，`record` 让 DTO 层代码量显著减少且天然不可变。
- Actuator + Micrometer 直接产出 SLO 所需指标，无需自建。
- spike 先行验证，启动期风险低。

**负面后果与缓解**
- 负面：Spring Boot 4.x 生态中部分第三方库（如 MyBatis-Plus）适配滞后，选型自由度受限。
- 缓解：本项目所需的核心依赖（MyBatis Starter 4.1.0、springdoc 3.1.1、Caffeine 3.3.1）均已明确版本且已验证；CI 增加"启动冒烟 + `/swagger-ui.html` 与 `/actuator/health` 探活"作业提前暴露不兼容。详见 02 概设 §14 风险 K7。

**相关代码位置**
- `backend/pom.xml`：`spring-boot-starter-parent` 版本、`<java.version>21</java.version>`
- `spike/pom.xml`：验证性 pom（4.1.1 + MyBatis 4.1.0 + H2）
- `backend/src/main/java/com/smartlib/SmartLibApplication.java`
- `backend/src/main/resources/application.yml`

---

## ADR-002

### 为何使用 MyBatis 而非 JPA

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
持久层有四种主流选择：MyBatis（XML/注解手写 SQL）、Spring Data JPA（Hibernate）、JdbcTemplate（裸 JDBC 封装）、MyBatis-Plus（MyBatis 增强）。本项目的两个核心叙事点——**库存扣减的条件更新正确性**与**慢查询治理（检索从 2.8s 优化到目标 < 300ms）**——都要求开发者对 SQL 有完全掌控。

**决策**
使用 **`mybatis-spring-boot-starter:4.1.0`**，SQL 以 XML（`resources/mapper/*.xml`）为主、简单查询用注解；不引入 JPA/Hibernate，不使用 MyBatis-Plus。

**理由**
1. **条件更新必须手写而且要写对**：库存扣减的正确性核心是 `update book_stock set available_copies = available_copies - #{n} where book_id = #{bookId} and available_copies >= #{n}`。这条语句的语义（`where` 中的下界判断 + 原子递减）是防超卖的根，MyBatis 让它以最直白的形式存在于代码里，便于 CodeReview 与设计讲解；JPA 下需要绕过 QueryDSL/原生 SQL 才能表达，反而增加理解成本。
2. **规避字节码增强风险**：Hibernate 的懒加载代理、一级/二级缓存、脏检查都会在调试期制造"看到的现象与代码不一致"的问题；本项目强调可预测、可解释，禁用运行期织入（02 概设约束 C3）。
3. **慢查询优化需要 SQL 级可见性**：`like '%kw%'` 全表扫描 120 万行（现状基线 2.8s）这类问题，只有在 SQL 由自己书写时，优化动作（改索引、改写法、换引擎）才是可控可讲的。
4. **已在 spike 验证**：`mybatis-spring-boot-starter:4.1.0` + H2 `MODE=MySQL` 的集成测试跑通，配置极简（只需 `map-underscore-to-camel-case: true`）。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| Spring Data JPA | 生成 SQL 难以精准控制；库存扣减需退回原生 SQL，反而丧失其"少写 SQL"的优势；字节码增强带来调试与启动期风险；与本项目"SQL 可控"目标冲突 |
| JdbcTemplate | SQL 完全可控，但样板代码多（手写 RowMapper、手工拼参数、无动态 SQL）、易出 SQL 注入；开发效率与本项目表数量之间的性价比低于 MyBatis |
| MyBatis-Plus | 单表 CRUD 效率高，但本项目表少（9 张）且关键 SQL 都要手写，其收益被摊薄；AR 模式与自动填充字段带来隐式行为；对 Spring Boot 4.1.1 的版本适配滞后是真实风险 |
| 自研轻量 DAO | 无必要，等于重造 MyBatis 且失去生态与社区验证 |

**正面后果**
- 扣减 SQL 与防超卖逻辑一一对应，评审与排障路径最短。
- 无字节码增强，堆栈与运行时行为完全可预测。
- 慢查询优化、索引有效性可逐条 SQL 验证并写进测试报告。

**负面后果与缓解**
- 负面：单表 CRUD 需要手写 SQL 与 XML，样板代码多；字段变更要同时改实体、XML、结果映射。
- 缓解：单表简单查询用 MyBatis 注解（`@Select/@Insert`）省去 XML；DTO 用 `record`；`map-underscore-to-camel-case: true` 消除大部分 `resultMap` 手写；CodeReview 清单包含"SQL 与索引是否匹配"检查项。

**相关代码位置**
- `backend/pom.xml`：`mybatis-spring-boot-starter` 版本 `4.1.0`
- `backend/src/main/java/com/smartlib/config/MyBatisConfig.java`
- `backend/src/main/resources/mapper/BookMapper.xml`、`BookStockMapper.xml`、`BorrowRecordMapper.xml`
- `backend/src/main/java/com/smartlib/mapper/BookStockMapper.java`（扣减条件更新 SQL 所在）
- `backend/src/main/resources/application.yml`：`mybatis.configuration.map-underscore-to-camel-case`

---

## ADR-003

### 为何不使用 Lombok

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
Java 实体与 DTO 的 getter/setter/构造器/`toString`/`equals` 样板代码量很大，Lombok 是业界常规解法。但 Lombok 依赖**编译期注解处理器 + 对 IDE/编译器内部 API 的侵入**，在 JDK 版本快速演进（本项目运行环境为 Java 25，字节码基线 21）与 Spring Boot 4.x 的组合下，属于潜在的不稳定因素。

**决策**
**不引入 Lombok**。实体手写 getter/setter（必要时用 `equals/hashCode`），所有 DTO 使用 **`record`**（Java 21 原生），构造使用显式构造器或 `record` 的规范构造器。

**理由**
1. **`record` 已覆盖 80% 的样板代码消除需求**：本项目 DTO 全部不可变（`BookQuery`、`BorrowCommand`、`BorrowResult`、`StockView`、`ReaderProfile`），`record` 原生提供构造器、访问器、`equals/hashCode/toString`，零依赖、零注解处理器。
2. **消除构建链风险**：Lombok 与编译器版本的耦合是长期痛点（新 JDK 上的处理器适配滞后），移除它意味着 `mvn test` 的失败原因集合显著收窄——这对一个要在 CI 上稳定全绿的项目价值很高。
3. **可读性优先**：实体只有 8 个（`Book`、`BookStock`、`Category`、`Reader`、`BorrowRecord`、`Reservation`、`StockLedger`、`RuleConfig`），手写访问器的绝对成本可控；显式代码在设计讲解与调试时无"魔法"。
4. **避免隐式行为**：`@Data`/`@Builder` 的默认语义（可变性、`equals` 用哪几个字段、`toString` 泄漏敏感字段）需要额外配置与认知成本。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| Lombok（`@Data`/`@Builder`） | 依赖注解处理器，与 JDK/编译器版本耦合，构建链风险高；隐式生成的 `equals/toString` 需要额外治理 |
| Kotlin（data class） | 引入第二语言，Java 岗位叙事被稀释，团队与 JDK 工具链复杂度上升 |
| IDE 生成代码后提交 | 与"禁 Lombok + 手写"实质相同，采纳其做法但要求统一风格（由 CodeReview 保证） |
| Java 25 的简化 main / 新特性替代 | 与字节码基线 21 冲突，不采用 |

**正面后果**
- 构建链只依赖 JDK 与 Maven，`mvn test` 失败原因收敛。
- DTO 天然不可变、线程安全，`record` 的 `equals/hashCode` 语义确定（适合做缓存 key 与集合元素）。
- 代码"所见即所得"，IDE 跳转与断点行为无差异。

**负面后果与缓解**
- 负面：实体 getter/setter 样板代码增加，改动字段时工作量大；构造器冗长。
- 缓解：① DTO 全用 `record`，只在实体上付出手写成本；② 实体字段变更集中在少数类（8 个），配合测试覆盖率兜底；③ CodeReview 清单增加"实体访问器是否完整、是否与列名映射一致"检查项；④ 如未来确需，可用 `@Accessors` 风格的显式 build 方法替代，仍不引 Lombok。

**相关代码位置**
- `backend/pom.xml`：**无** `lombok` 依赖（作为约束被显式排除）
- `backend/src/main/java/com/smartlib/domain/entity/*.java`：手写访问器
- `backend/src/main/java/com/smartlib/domain/dto/*.java`：`record` 定义
- [`docs/05-编码规范与工程实践.md`](./05-编码规范与工程实践.md)：禁 Lombok 的规范条款

---

## ADR-004

### 库存扣减默认选择 DB 条件更新（S2）

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
痛点 P1：开学季热门教材多窗口同时借阅，`available_copies` 被扣成负数，2024 年投诉 37 起。蓝图 §6.1 给出 6 种候选：S1 单机锁、S2 DB 条件更新、S3 DB 悲观锁、S4 乐观锁 version 自旋、S5 Redis+Lua 原子预扣、S6 MQ 串行化。必须选定一个**默认**方案，并给出切换与降级路径。

**决策**
默认使用 **S2：DB 条件更新**（`OptimisticDbStockStrategy`），核心语句：

```sql
update book_stock
   set available_copies = available_copies - #{n},
       updated_at = now()
 where book_id = #{bookId}
   and available_copies >= #{n}
```

`affected = 1` 视为扣减成功，`affected = 0` 视为库存不足（业务失败，非异常）。通过 `smartlib.stock.strategy` 配置切换 S1~S6；S5 检测到 Redis 不可用时自动降级 S2。

**理由**
1. **正确性来自单条原子语句**：`where available_copies >= n` 与递减在同一条 UPDATE 内由数据库保证原子性，不依赖应用层锁、不依赖缓存、不存在检查与扣减之间的时间窗——这正是超卖的根因所在。
2. **组件最少 = 故障面最小**：不引入 Redis（默认路径不依赖）、不引入 MQ，主链路只有应用与 MySQL，符合蓝图约束 C6。
3. **性能对本项目场景足够**：单热点教材上确实存在行级串行，但借阅事务极短（目标 P99 < 200ms，见蓝图 §7），单实例 200 QPS 目标可在 4C8G 上达成；本项目的真实矛盾（见 02 概设 §13.1）是峰值口径而非扣减吞吐。
4. **实现最简单、可讲解性最强**：`affected` 的 1/0 语义、事务边界、与 `stock_ledger` 的同事务写入，全部可以白盒展示。
5. **它还是降级终点**：S1/S3/S4/S5/S6 在依赖不可用或冲突过高时，都能安全退回 S2。

**被否决的替代方案及否决原因**（作为默认方案）

| 方案 | 否决原因 |
|---|---|
| S1 单机锁 | 多实例部署下完全失效，与"高可用多实例"目标直接冲突；只能用于本地教学对比 |
| S3 悲观锁 `for update` | 持锁时间长（校验 + 写流水 + 事务提交），热点书上锁等待放大，易触发锁超时与死锁 |
| S4 version 自旋 | 无长事务，但热点冲突时重试放大（200 并发抢 10 册可能出现大量重试），失败率与延迟更不可控 |
| S5 Redis+Lua | 抗并发最好，但引入"Redis 已扣、DB 未落"的最终一致窗口，需补偿与对账，主链路复杂度显著上升；本项目常规借还并不需要秒杀级吞吐 |
| S6 MQ 串行化 | 链路最长，端到端变最终一致，用户点"借书"要等异步结果；本项目以"同步明确结果"为体验优先 |

**正面后果**
- 零外部依赖，超卖数目标为 0 且有明确机制保证。
- 事务边界清晰：`book_stock` 扣减 + `borrow_record` + `stock_ledger` 同事务，天然满足流水守恒。
- 作为所有其它方案的降级终点，简化的降级矩阵。

**负面后果与缓解**
- 负面：单热点书目（如某本指定教材）上形成行级串行，高峰排队时延上升；DB 成为唯一关键路径。
- 缓解：① 明确验收线：结果正确性优先，P99 目标 < 200ms，超出则压测暴露并记录；② 秒杀式抢书活动切 S5（配置热更）；③ 对超热书目按 `book_id` 做应用层限流/排队提示；④ 保留 S6 演示削峰路径。详见 02 概设 §8.3、§14（风险 K4）。

**相关代码位置**
- `backend/src/main/java/com/smartlib/service/stock/StockStrategy.java`（SPI）
- `backend/src/main/java/com/smartlib/service/stock/OptimisticDbStockStrategy.java`（默认实现）
- `backend/src/main/java/com/smartlib/service/stock/StockStrategyFactory.java`
- `backend/src/main/java/com/smartlib/config/StockStrategyConfig.java`
- `backend/src/main/resources/mapper/BookStockMapper.xml`
- `backend/src/test/java/com/smartlib/BorrowConcurrencyTest.java`（200 线程抢 10 册）
- `backend/src/main/resources/application.yml`：`smartlib.stock.strategy`

---

## ADR-005

### 为何自研限流熔断而非直接上 Sentinel

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
蓝图 §6.3 要求入口限流、熔断降级与兜底。候选实现有三条路：自研令牌桶 + 熔断状态机、引入 Resilience4j、引入阿里 Sentinel。项目还要在多处使用这两类能力（公共 API、内部 Agent 接口、DB/Redis/检索依赖），因此需要一个"能在任意调用点复用"的抽象。

**决策**
**自研** `TokenBucketRateLimiter` + `CircuitBreaker`（CLOSED/OPEN/HALF_OPEN 三态）+ `DegradeFallback`，放在 `com.smartlib.support` 包；同时**保留 Resilience4j 的等价配置**写入 [`docs/09-多种解决方案对比手册.md`](./09-多种解决方案对比手册.md) 作为对照方案。

**理由**
1. **项目要证明的是"理解原理"而非"会引依赖"**：自研状态机可以白盒讲解失败率窗口、冷却时间、半开探测计数、令牌补充速率与突发容量，面对追问时的说服力显著更强。
2. **需求面窄，自研代码量可控**：本项目只需要令牌桶限流 + 三态熔断 + 降级兜底，不需要 Sentinel 的热点参数限流、集群流控、控制台规则推送等重型能力；自研总量在数百行量级。
3. **零新增依赖 = 零版本适配风险**：Resilience4j/Sentinel 对 Spring Boot 4.1.1 的适配需要额外验证，而自研只依赖 JDK 与 Spring 核心；这与ADR-003 的取舍逻辑一致（收窄构建链风险）。
4. **需要按作用域定制**：内部 `/internal/agent/**` 需要与公共 API 隔离的独立桶，依赖级熔断需要按 `name` 独立状态（DB、Redis、检索、Agent→后端各自一个），自研更容易与指标（`smartlib_circuit_state`、`smartlib_rate_limited_total{scope}`）直接绑定。
5. **可降级性要求**：Redis 不可用时集群限流要能退化为进程内桶，自研实现能把这条降级语义写死并测到。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| Sentinel | 功能最全，但需引入依赖 + 可选控制台部署，运维与版本适配成本最高；其核心优势（控制台可视化、集群流控）在本项目用不上 |
| Resilience4j | 开箱即用、质量高，但引入依赖且以注解/配置为主，讲述时偏"我会用"，缺少白盒可解释性；作为对照方案保留更合适 |
| 仅依赖 Nginx `limit_req` | 只能做入口粗粒度限流，无法做依赖级熔断与业务级降级，覆盖不了蓝图 §6.3 A5 |
| 完全不限流（靠扩容） | 与"保护 DB"的目标冲突，DB 一旦被压垮全馆停摆（对应痛点 P3） |

**正面后果**
- 限流与熔断完全白盒，可写单测覆盖三态迁移与边界条件，讲述时能给出实现细节。
- 零依赖，构建链与启动时间不受影响；指标命名与业务作用域直接对齐。
- Redis 缺失时的降级语义（集群桶 → 进程内桶）由自己定义并测试。

**负面后果与缓解**
- 负面：自研正确性风险（状态机 bug、并发计数错误、时钟回拨影响令牌补充）；缺少成熟的可视化控制台。
- 缓解：① 单测覆盖令牌桶容量/补充速率/突发的边界，覆盖熔断 CLOSED→OPEN→HALF_OPEN→CLOSED 全路径与并发场景；② 灰度期以"仅观察不拦截"模式运行，对照真实指标再开启拦截；③ 指标暴露到 Prometheus + Grafana 面板替代控制台；④ 一旦自研出现问题，可退回保留的 Resilience4j 配置（对照方案已写好）。详见 02 概设 §14（风险 K2）与 §15（评审意见 7）。

**相关代码位置**
- `backend/src/main/java/com/smartlib/support/TokenBucketRateLimiter.java`
- `backend/src/main/java/com/smartlib/support/CircuitBreaker.java`
- `backend/src/main/java/com/smartlib/support/DegradeFallback.java`
- `backend/src/main/java/com/smartlib/support/GracefulShutdown.java`
- `backend/src/main/resources/application.yml`：`smartlib.resilience.*`
- `docs/09-多种解决方案对比手册.md`：Resilience4j 对照配置

---

## ADR-006

### 检索引擎为何以自研倒排索引为默认、ES 为可选

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
痛点 P2：读者搜索"计算机网络 谢希仁"，现状系统用 `like '%关键词%'` 全表扫描 120 万行，平均 2.8s、峰值超时 8s，且无分词、无拼音、无相关度排序。蓝图 §6.2 给出 5 个候选：Q1 LIKE、Q2 前缀索引、Q3 自研倒排、Q4 MySQL ngram 全文、Q5 Elasticsearch。目标：检索 P99 < 300ms、召回率 ≥ 95%（SLO）。

**决策**
默认使用 **Q3 自研倒排索引**（`InvertedIndexSearchEngine`）：ngram 分词 + 拼音首字母 + 倒排求交 + BM25 相关度打分 + TopK 小顶堆；`SearchEngine` SPI 暴露 `smartlib.search.engine` 配置项，**Q5 Elasticsearch 作为可选实现**（数据量 > 500 万或需要复杂相关度时切换），Q1 保留为降级兜底与性能基线对照。

**理由**
1. **零外部依赖即可达成目标**：检索目标 P99 < 100ms（蓝图 §6.2 Q3 列），而内存倒排在 58 万种书目、约 150MB 索引估算（02 概设 §13.4）的规模下可达成；不需要为 58 万行数据上一套 ES 集群。
2. **可解释的召回质量提升**：ngram（2-gram）解决"子串匹配不到词"的问题，拼音首字母解决"jisuanji → 计算机"的输入习惯，BM25 解决"按相关度排序"——这三点恰好对症 P2 的三个缺陷，且每一点都能白盒讲解与单测验证。
3. **离线可运行是硬约束**（蓝图约束 C6、ADR-011 同类逻辑）：无 ES 集群、无网络也能跑通 `mvn test` 与 `SearchBenchmarkTest`。
4. **SPI 让"选型"变成"配置"**：切换只改 `smartlib.search.engine`，不触碰 `BookSearchService` 调用方，因此默认选 Q3 **不会造成锁定**；ES 作为能力上限的演进路径随时可用。
5. **写放大与运维成本**：Q4 的 ngram 全文索引在书目批量导入时有明显写放大；Q5 需独立集群与运维。两者都不适合作为本项目的默认。

**被否决的替代方案及否决原因**（作为默认）

| 方案 | 否决原因 |
|---|---|
| Q1 `like '%kw%'` | 现状基线 2.8s（120 万行全表扫描），是待治理的**问题本身**，不是方案；仅保留为降级兜底与对照基线 |
| Q2 前缀索引 | 目标约 200ms，但只支持前缀匹配、召回质量差（"谢希仁"能查、"计算机网络"中的"算机"查不到），无相关度 |
| Q4 MySQL ngram FULLTEXT | 目标约 120ms 可用，但分词能力受引擎限制（无拼音、相关度不可控），且批量导入写放大明显；作为对照保留 |
| Q5 Elasticsearch（作为默认） | 目标约 50ms 最强，但需独立集群（内存/运维/版本升级），对 58 万种书目属过度设计；且破坏"零依赖离线可跑"约束 |
| 外接第三方搜索 API | 引入网络依赖与费用，不可离线，且数据出域有安全顾虑 |

**正面后果**
- 检索目标可达且零依赖，CI 可离线跑基准测试产出对比表。
- 算法（分词、倒排、BM25、TopK）完全可讲解，是项目最有技术含量的叙事点之一。
- 通过 SPI 保留 ES 演进路径，不存在"选错锁死"的风险。

**负面后果与缓解**
- 负面：① 索引需常驻内存（约 150MB 估算），启动需构建索引；② 相关度调参需要人工标注集；③ 自研实现的分词质量弱于成熟引擎（无同义词、无词干还原）。
- 缓解：① 索引内存目标 ≤ 200MB 并加 JVM 内存告警；启动构建目标 ≤ 30s（**待实测回填**），必要时改异步构建 + 就绪探针阻塞直到完成；② 建立人工标注集作为召回率 ≥ 95% 的验收依据；③ 同义词表以配置方式补充，不硬编码；④ 一旦数据量或质量要求超出，切 Q5（配置级切换）。详见 02 概设 §14（风险 K3）与 §15（评审意见 2）。

**相关代码位置**
- `backend/src/main/java/com/smartlib/service/search/SearchEngine.java`（SPI）
- `backend/src/main/java/com/smartlib/service/search/InvertedIndexSearchEngine.java`（默认）
- `backend/src/main/java/com/smartlib/service/search/LikeSearchEngine.java`（降级兜底/基线）
- `backend/src/main/java/com/smartlib/service/search/PrefixIndexSearchEngine.java`、`NgramFullTextSearchEngine.java`、`ElasticsearchSearchEngine.java`
- `backend/src/main/java/com/smartlib/service/BookSearchService.java`
- `backend/src/main/java/com/smartlib/cache/BloomFilter.java`、`CacheGuard.java`
- `backend/src/test/java/com/smartlib/SearchBenchmarkTest.java`
- `backend/src/main/resources/application.yml`：`smartlib.search.engine`

---

## ADR-007

### 为何将 `book` 与 `book_stock` 分表

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
书目（`book`）与库存（`book_stock`）是 1:1 关系。合并成一张表可以减少一次 JOIN，看起来更简单；但库存是**高频写**对象（每次借还都写），书目是**读多写少**对象（58 万行，可大比例缓存），二者的读写特征、一致性要求、审计要求完全不同。

**决策**
**分表**。`book` 保存书目元数据（`isbn`、`title`、`author`、`publisher`、`category_id`、`publish_year`、`price`、`tags`、`total_copies`），`book_stock` 保存库存（`book_id` 主键、`total_copies`、`available_copies`、`locked_copies`、`version`、`updated_at`）。`book_stock` 是**扣减唯一入口**。

**理由**
1. **缓存策略必须不同**：`book` 可以 30min 长 TTL 甚至全量缓存；`book_stock` **不能作为缓存里的唯一事实源**（业务约束 B2），其缓存 TTL 只能秒级且写后立即删。同表会导致"要么书目缓存太短、要么库存缓存太长"，两头都不对。
2. **行锁范围与页争用隔离**：库存更新 `update book_stock ... where book_id = ?` 只锁一行窄记录；若与含 `tags` 等宽字段的书目同表，热点行更新会与宽行读写争用页与缓冲池，且行更宽导致页分裂更频繁。
3. **职责单一与变更频率隔离**：库存行的生命周期（高频更新、需要审计与对账）与书目行（低频更新、需要缓存与检索重建）完全不同，治理策略无法共用。
4. **审计闭环更清晰**：`stock_ledger` 以 `book_id` 关联库存变化，`sum(delta)` 必须等于 `available_copies` 的变化量（业务约束 B7）；分表后"库存事实源"只有一个对象，对账口径明确无歧义。
5. **容量与归档独立**：`book_stock` 约 58 万行 / 约 60MB，`book` 约 725MB（估算见 02 概设 §13.3），可各自归档与优化。
6. **安全边界收窄**：扣减路径只允许触碰 `book_stock` + `stock_ledger`，从表结构上降低误改书目元数据的风险。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| 单表（库存字段并入 `book`） | 缓存语义冲突（书目要长 TTL、库存要短 TTL 且不可作事实源）；热点更新与宽行读写争用；审计对象不唯一；扣减安全边界变宽 |
| 分表但库存放缓存为准（Redis 为准，异步落库） | 直接违反业务约束 B2，把超卖风险从"DB 条件更新"转移到"缓存一致性"，与 P1 的治理目标相反 |
| 库存拆到独立库/独立表空间 | 58 万行、约 60MB 的规模完全不必要，且跨库事务会破坏"同事务写 `stock_ledger`"的强审计设计 |
| 库存字段拆成"每册一行"（copy 级表） | 语义更细（可追踪单册条码），但 120 万行并放大扣减与预约逻辑复杂度；本版不做单册条码管理（范围外） |

**正面后果**
- 缓存策略与锁粒度各得其所，超卖治理路径最清晰。
- `stock_ledger` 守恒校验有唯一事实源，对账任务可自动化。
- 库存表小且窄，扣减语句的索引与执行计划长期稳定。

**负面后果与缓解**
- 负面：① 书目详情需 JOIN，存在 JOIN 退化风险；② 跨表一致性依赖事务（无法靠单表原子性）；③ DDL 变更需要协调两表。
- 缓解：① 详情接口走 L1+L2 两级缓存，JOIN 只在缓存未命中时执行，并对该路径做 P99 验收（02 概设 §15 评审意见 5）；② 扣减 + 借阅记录 + 流水在**同一事务**内完成，单库单实例下 3 年内无跨库问题；③ 若 JOIN 被证明是瓶颈，冗余 2~3 个纯展示字段到 `book_stock` 属可逆优化，但必须新增 ADR。

**相关代码位置**
- `backend/src/main/java/com/smartlib/domain/entity/Book.java`、`BookStock.java`
- `backend/src/main/java/com/smartlib/mapper/BookMapper.java`、`BookStockMapper.java`
- `backend/src/main/resources/mapper/BookMapper.xml`、`BookStockMapper.xml`
- `deploy/sql/01-schema.sql`
- [`docs/04-数据库设计.md`](./04-数据库设计.md)：完整 DDL 与索引依据
- [`docs/02-概要设计.md`](./02-概要设计.md) §7.3

---

## ADR-008

### 为何采用 H2 + MySQL 双环境（H2 使用 `MODE=MySQL`）

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
项目要在三种场景运行：① 本机开发与 `mvn test`（要求零外部依赖、秒级启动、可反复重建）；② 演示（要求单命令启动）；③ 生产（要求 MySQL 8.0 的真实行为）。痛点 P3 的治理又要求多实例部署，因此测试环境必须是"每个实例都能独立重建"的形态。

**决策**
local/demo 使用 **H2 2.5.252 内存库并强制 `MODE=MySQL`**；prod 使用 **MySQL 8.0**。两者共用同一套 `deploy/sql/01-schema.sql` 与同一套 MyBatis SQL。连接串示例：

```
jdbc:h2:mem:smartlib;DB_CLOSE_DELAY=-1;MODE=MySQL;DATABASE_TO_LOWER=TRUE
```

**理由**
1. **测试与 CI 零依赖、零等待**：H2 内存库让 `mvn test` 不需要 Docker/MySQL，单次构建稳定且可重复；`sql.init.mode: always` 可在每次启动重建 schema，天然满足"并发测试需要干净起点"的需求。
2. **`MODE=MySQL` 把方言差异压到最小**：启用 MySQL 兼容模式后，`AUTO_INCREMENT`、`LIMIT`、`ON DUPLICATE KEY`、反引号标识符、`DATETIME` 等常用语法可共用同一份 DDL/SQL，使"本地过、线上挂"的风险显著下降。
3. **已在 spike 验证**：`spike/src/main/resources/application.yml` 用该连接串跑通了 MyBatis + REST + 集成测试（含 409 扣减失败断言），不是纸面方案。
4. **生产真实性必须保留**：H2 无论如何兼容都不是 MySQL——锁行为、执行计划、`FULLTEXT`、`information_schema`、字符集排序规则都不同。生产必须用 MySQL 才能验证索引与并发假设。
5. **与 P3 高可用演练配合**：H2 内存库便于起多个后端实例做负载均衡演示；生产切 MySQL 后多实例连同一主库，演练才有意义。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| 只用 MySQL（含本地/测试） | 测试与 CI 强依赖外部服务，启动慢、易 flaky，且并发测试需要额外的清理脚本 |
| 只用 H2（含生产） | 无法验证真实 MySQL 的锁、索引与执行计划，容量与并发结论不可信；JDBC URL 里的方言兼容性在真实负载下会暴露问题 |
| H2 默认模式（不设 `MODE=MySQL`） | SQL 方言差异大（标识符大小写、自增、`LIMIT` 语法等），会出现本地过线上挂 |
| Testcontainers 启动真实 MySQL | 保真度最高，但需要 Docker 且每次测试启动容器，构建时间与 CI 复杂度上升；作为**可选的 MySQL 集成测试作业**保留（见缓解措施），不作为默认 |
| SQLite / Derby | 与 MySQL 方言差异更大，生态与目标生产库不符 |

**正面后果**
- `mvn test` 毫秒级启动、可重复、无外部依赖，CI 稳定。
- 同一套 DDL/SQL 双环境复用，迁移成本低。
- 演示档可"一条命令起全栈"，符合蓝图 M5 阶段门要求。

**负面后果与缓解**
- 负面：H2 与 MySQL 存在真实行为差异——`FULLTEXT ... WITH PARSER ngram`（H2 不支持）、`ON DUPLICATE KEY UPDATE` 的细节、锁与隔离级别、`information_schema`、排序规则与大小写敏感性、部分函数（如 `DATE_ADD` 变体）。本地全绿不等于线上可用。
- 缓解：① 差异点在设计阶段显式列出（本条 + 02 概设 §14 风险 K8），任何使用 MySQL 专有能力的 SQL 必须在 `@Profile("!local")` 或引擎实现层隔离；② CI 增加**独立作业**：用 MySQL 8.0 容器跑同一套集成测试（相当于 Testcontainers 的落地形态），与 H2 作业并行；③ Q4（ngram 全文）默认不启用，仅在 MySQL 环境启用并可回退；④ 生产发布前用 MySQL 跑一遍并发用例（200 线程抢 10 册）。

**相关代码位置**
- `backend/src/main/resources/application-local.yml`（H2 连接串）
- `backend/src/main/resources/application-prod.yml`（MySQL 连接串）
- `spike/src/main/resources/application.yml`：已验证的 H2 `MODE=MySQL` 配置
- `deploy/sql/01-schema.sql`（双环境共用）
- `deploy/docker-compose.yml`（MySQL 8.0 服务；CI 的 MySQL 集成作业复用）
- `backend/src/test/java/com/smartlib/BorrowConcurrencyTest.java`

---

## ADR-009

### 为何幂等落在 `idempotent_record` 表而非纯 Redis

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
`POST /api/borrows` 必须携带 `Idempotency-Key`（业务约束 B3），且 Agent 侧工具调用（`borrow`/`renew`/`reservations`）在重试时会重复发起同一业务请求。幂等实现有两条主流路径：**纯 Redis**（`SETNX idem_key 1 EX 86400`）与**落库表**（唯一键约束）。蓝图 §5 已定义 `idempotent_record(idem_key, biz_type, result_json, created_at)` 表。

**决策**
采用 **落库表为主**的实现：`IdempotencyAspect` 以 `insert into idempotent_record(idem_key, biz_type, created_at)` 抢占，唯一键冲突即判定为重放并读回 `result_json` 返回**与首次完全一致的结果**；`Redis` 仅作为**可选的快速前置判定**（命中即短路，未命中仍走表），Redis 不可用时功能不受影响。

**理由**
1. **幂等要求与业务事务同生共死**：借阅是"扣减 + 写借阅记录 + 写库存流水"的事务。幂等标记若存在 Redis，会出现"DB 事务回滚但 Redis 已标记 → 用户重试被误判为重放，拿不到任何结果"或"DB 已提交但 Redis 标记丢失 → 重试导致重复借出"。把幂等标记放进同一数据库，才能与业务事务共享"提交或回滚"的语义。
2. **唯一键就是并发锁**：`uk(idem_key)` 让并发重复请求中只有一个 `insert` 成功，天然是分布式安全的互斥，无需额外锁；Redis 的 `SETNX` 在 Redis 故障切换/主从延迟下存在丢标记风险。
3. **可返回相同结果**：`result_json` 落库后，重放请求能拿到与首次**完全相同**的 `borrowId`，这正是蓝图 §7「同一 `Idempotency-Key` 重复请求只生效一次」的验收口径；纯 Redis 需要额外持久化结果体，反而更重。
4. **硬约束 C5（Redis 可降级）**：幂等是正确性能力，不能依赖可选组件。落库表使 Redis 完全缺席时幂等依然成立。
5. **可审计与可追溯**：`idempotent_record` 留下了"何时、哪类业务、被重放了几次"的证据（配合 `smartlib_idempotent_replay_total` 指标），排障时可直接查表。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| 纯 Redis `SETNX` | 与业务事务不在同一资源上，存在"标记与结果不一致"窗口；Redis 故障/主从延迟下丢标记会导致重复借出；违反 Redis 可降级约束 |
| 纯 Redis + 结果体持久化 | 需要手工把结果写 Redis 并处理过期、序列化、内存占用；复杂度高于直接用表 |
| 数据库唯一索引"天然去重"（如 `stock_ledger.uk(biz_no)`） | 可以防重复扣减/重复流水，但**不能返回首次结果**，客户端拿不到 `borrowId`；只能作为最后一道防线，不能替代幂等接口语义 |
| 只靠客户端不重试 | 不可依赖；网络超时后客户端无法区分"未执行"与"执行成功但响应丢失"，必然需要服务端幂等 |
| 分布式锁串行化整个借阅 | 锁粒度过粗（应按 `idem_key` 而非按书或全局），且引入 Redis 强依赖 |

**正面后果**
- 幂等与业务同事务，语义最强：要么都生效，要么都不生效。
- 唯一键即锁，实现简单、无额外组件；Redis 缺席时能力不降。
- 重放返回一致结果，客户端重试体验确定；重放次数可观测、可审计。

**负面后果与缓解**
- 负面：① 每次借阅多一次 `insert` 与最终 `update result_json`，热点时增加 DB 写压力（`idem_key` 唯一索引争用）；② 表会持续增长；③ 幂等有效期受清理策略约束。
- 缓解：① 幂等表是**窄表 + 内存级唯一索引**，开销远小于扣减本身；② 定时清理任务按 `created_at` 删除 24 小时前记录（稳态约万行量级，见 02 概设 §13.3）；③ 用 `smartlib_idempotent_replay_total` 监控重放率，异常升高视为客户端重试策略问题并告警；④ Redis 可用时用 `SET NX` 做**前置快速判定**降低 DB 读，但正确性始终由表保证（快速路径只是优化）。

**相关代码位置**
- `backend/src/main/java/com/smartlib/common/IdempotencyAspect.java`
- `backend/src/main/java/com/smartlib/common/ErrorCode.java`：`10005 IDEMPOTENT_CONFLICT`
- `backend/src/main/resources/mapper/IdempotentRecordMapper.xml`（或注解 Mapper）
- `deploy/sql/01-schema.sql`：`idempotent_record` 表与 `uk(idem_key)`
- `backend/src/main/java/com/smartlib/web/AgentToolController.java`（内部工具幂等入口）
- `agent-service/app/client/java_client.py`（重试时复用同一 `idempotencyKey`）

---

## ADR-010

### Agent 为何使用自研 ReAct 循环而非 LangChain

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
蓝图 §6.4 要求 Agent 具备：Function Calling 工具调用（G2）、RAG 检索增强（G3）、Plan-and-Execute 多步编排（G4）、规则路由 + LLM 兜底（G5），并附带工程护栏（工具白名单、参数校验、超时/重试/熔断、无工具证据不下结论、低置信度转人工、trace 透传、SSE 流式、Token 与延迟埋点）。候选实现：自研 ReAct 循环、LangChain、LangGraph、Dify 平台。

**决策**
**自研 Agent 循环**（`agent/runtime.py`），支持 **ReAct 与 Plan-Execute 双模式**：`intent.py` 规则优先识别意图，`planner.py` 做多步拆解，`executor.py` 负责工具校验/超时/重试/熔断，`guardrail.py` 做输出前护栏；Prompt 全部集中在 `prompts.py`。

**理由**
1. **项目要证明"理解 Agent 原理"**：ReAct 循环的 Thought→Action→Observation、终止条件（任务完成/轮次上限/超时预算）、上下文裁剪、工具选择失败的回退——这些是本项目最有讲述价值的工程内核。框架会把这些隐藏在抽象层之后，讲解变成"我调了框架"。
2. **护栏需要最细粒度的钩子**：`无工具证据不下结论`（业务约束 B9）必须在"生成最终回答之前"检查本次会话是否真的有过成功的工具调用及其返回内容。自研循环里这是一个明确的位置；框架里需要定制回调/后处理，反而更绕。
3. **依赖风险与离线约束**：LangChain 依赖树庞大且版本变动频繁，与"`pytest` 全绿、无 Key 可离线跑"（约束 C6/C7）相冲突。自研只依赖 FastAPI + Pydantic v2 + httpx。
4. **双模式与规则优先路由的定制需求**：`intent.py` 要求"高频意图走规则（快、便宜、稳），长尾走 LLM"，这与框架默认的"全走 LLM"路径不同，自研实现更直接。
5. **可观测性对齐**：`trace_id` 透传、工具调用埋点、首 Token 延迟、Token 计数都要与 Java 侧指标体系统一命名，自研更容易做到精确埋点。
6. **规模匹配**：本项目工具只有 7 个（`search_books`、`get_stock`、`borrow`、`renew`、`my_loans`、`policy_qa`、`recommend`），不需要复杂图编排能力。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| LangChain | 抽象层厚、栈追踪与调试困难；依赖树庞大、版本变动频繁，与离线可跑约束冲突；护栏需要定制回调实现，反而更复杂 |
| LangGraph | 图编排能力最强，但本项目流程深度有限（最多 3 步串行），引入状态机框架属过度设计；同样存在依赖与离线问题 |
| Dify / Coze 平台 | 平台方掌握 Prompt、记忆与工具定义，可解释性与可控性最低；需额外部署平台，破坏"零依赖可演示"；且无法讲清循环实现 |
| 纯 Prompt 直答（G1） | 是基线对照组：会编造库存与位置，幻觉率高，不满足"事实性回答有工具证据比例 100%"的硬约束 |
| 直接用 LLM 原生 function calling 无循环 | 单轮工具调用可用，但无法处理 G4 的复合任务（借书 + 推荐 + 查逾期），缺少多步 Observation 回填 |

**正面后果**
- 循环、Prompt、护栏、终止条件全部白盒，可讲解、可单测、可精确埋点。
- 零框架依赖：`pytest` 离线全绿，CI 稳定，无版本升级冲击。
- 规则优先路由让高频意图（借/续/查/规则）快、便宜、稳定，符合 G5。

**负面后果与缓解**
- 负面：① 需自行实现上下文管理与轮次/超时保护，否则可能死循环或上下文溢出；② 缺少框架的现成连接器与生态（如内建多模型适配）；③ 复杂图编排能力弱于 LangGraph。
- 缓解：① `runtime.py` 强制"最大轮次 + 总超时预算"双终止条件，超限即收敛为"抱歉，需要人工协助"并转人工；② 上下文按"系统 Prompt + 最近 N 轮 + 本轮工具结果"裁剪，丢弃中间冗余 Observation；③ 多模型适配由 `llm/router.py` 的 provider 抽象承担（已支持 mock 与 openai_compat）；④ 如未来流程显著复杂化，可新增 ADR 引入 LangGraph 承载编排层，保留自研工具与护栏层。

**相关代码位置**
- `agent-service/app/agent/runtime.py`（ReAct / Plan-Execute 主循环）
- `agent-service/app/agent/intent.py`、`planner.py`、`executor.py`、`prompts.py`、`guardrail.py`
- `agent-service/app/tools/registry.py`（8 个工具 + 白名单）
- `agent-service/app/api/chat.py`（SSE 流式）
- `agent-service/app/client/java_client.py`（trace 透传 + 熔断重试）
- `agent-service/app/eval/run_eval.py`（意图/工具/参数/端到端/延迟 5 项指标）
- `agent-service/tests/test_agent_e2e/`

---

## ADR-011

### Agent 为何默认使用 MockLLM 并可离线运行

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
Agent 服务的真实能力依赖 LLM（DeepSeek/通义/本地 vLLM）。但一个简历项目必须满足：陌生人 clone 后能立刻跑起来、CI 能在无 Key 环境下全绿、演示不因网络/额度/供应商故障而中断。同时，评测（`run_eval.py`）需要可重复的确定性输入输出。

**决策**
通过 `LLM_PROVIDER` 配置在 `mock`（默认）与 `openai_compat` 之间切换。`llm/mock.py` 实现**确定性规则模拟**（基于规则解析用户语句 → 选择工具 → 生成模板化回答），`llm/router.py` 在 `openai_compat` 调用失败时**自动回退 mock**。评测报告**分 provider 出具**。

**理由**
1. **零 Key 可完整演示**：`mvn test`、`pytest`、Docker Compose 起全栈、SSE 流式对话、8 个内部工具调用、护栏拦截、评测脚本——全部无需 API Key，符合约束 C7。
2. **CI 必须确定性**：真实 LLM 输出不确定，无法做断言。MockLLM 是确定性函数，可对"意图识别 → 工具选择 → 参数构造 → 回答结构"做精确断言，让 `pytest` 成为真正的回归网。
3. **评测需要可对比基线**：MockLLM 给出"工程链路正确率"的上界基线（链路本身没问题时，Mock 应当接近满分）；真实 provider 的差距才反映模型能力，二者分离才能定位问题在链路还是在模型。
4. **演示与故障的韧性**：课堂演示或评审现场网络不可控。`openai_compat` 失败自动回退 mock，保证"永远能演示完"。
5. **实现成本极低、收益明确**：MockLLM 只需覆盖有限意图与 8 个工具的确定性选择，代码量小；换来的是可测试性与可演示性的巨大提升。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| 强制要求真实 API Key | 陌生人无法运行、CI 需要密钥（泄漏风险）、演示受网络与额度制约 |
| 录制真实 LLM 响应做回放（VCR 模式） | 需要先有 Key 与网络才能录制，录制文件与 Prompt 变更强耦合、易失效；MockLLM 的规则实现更稳定，且不暴露真实密钥 |
| 使用本地小模型（如本地 vLLM + 小参数模型） | 需要 GPU/大内存，破坏"零依赖可运行"；作为 `openai_compat` 的一个 base_url 目标保留 |
| 只做纯 Prompt 直答的假 Agent | 无法验证工具调用、护栏、幂等等工程能力，等于放弃 G2/G5 的验证 |
| 免费额度第三方 LLM | 额度不确定、限速、可用性不可控，不适合作为默认路径 |

**正面后果**
- Clone 即跑：无 Key、无网络、无 GPU 也能完整演示与测试。
- CI 完全确定性，`pytest` 可作为回归网长期存在。
- 评测基线清晰：链路正确率（mock）与模型能力（真实 provider）分离，指标异常时能快速定位层次。

**负面后果与缓解**
- 负面：① MockLLM 的行为与真实模型差异大，**不能代表真实能力**（工具选择策略、参数抽取、拒答能力都不同）；② 存在"自测全绿但真实体验差"的风险；③ 双 provider 让行为分叉，维护两套路径。
- 缓解：① 评测报告**必须分 provider 出具**，能力指标（意图准确率 ≥ 92%、工具参数正确率 ≥ 95%、端到端 ≥ 88%、首 Token < 1.5s）以真实 provider 为准；无 Key 环境下明确标注"能力指标未测，仅链路验证"，**禁止用 mock 数据冒充实测**；② `prompts.py` 集中管理 Prompt，使两 provider 共享同一份指令，减少分叉；③ CI 保留一个"有 Key 才执行"的可选作业，用于定期跑真实 provider 基准；④ 详见 02 概设 §15（评审意见 10）。

**相关代码位置**
- `agent-service/app/llm/base.py`（LLM 抽象）
- `agent-service/app/llm/mock.py`（确定性 MockLLM，默认）
- `agent-service/app/llm/openai_compat.py`（真实 provider）
- `agent-service/app/llm/router.py`（选择与失败回退）
- `agent-service/app/config.py`：`LLM_PROVIDER`（默认 `mock`）
- `agent-service/app/eval/run_eval.py`、`metrics.py`、`dataset.jsonl`
- `agent-service/tests/`（依赖 mock，离线可跑）

---

## ADR-012

### 为何引入 `stock_ledger` 库存流水做强审计

| 项 | 内容 |
|---|---|
| **状态** | 已接受 |
| **日期** | 2026-10-16 |
| **决策者** | 架构评审 |

**背景**
痛点 P1（超卖）的**真实可观测症状**是 `available_copies` 变成负数或与"实际可借"不一致，但仅看 `book_stock` 的当前值无法回答"是谁、什么时候、因为什么把库存改成了这样"。蓝图 §5 明确要求：`stock_ledger` 保证"每一次扣减都有据可查"，是排查超卖的兜底手段（`sum(delta)` 必须等于 `available_copies` 的变化量），且 `stock_ledger` 带 `uk(biz_no)`、`idx(book_id, created_at)`、`trace_id` 字段。

**决策**
**所有**库存变更（借出、归还、续借相关锁定、预约锁定/释放、丢失核销、盘点调整、S5 补偿回补）都必须**在业务事务内**写入一条 `stock_ledger`，记录 `op_type`、`delta`、`before_qty`、`after_qty`、`biz_no`（唯一，幂等）、`trace_id`。定时对账任务校验 `sum(delta)` 与库存变化量一致；不一致即 P1 告警并冻结该书借还。

**理由**
1. **审计是对"零超卖"承诺的可验证证据**：没有流水，"超卖数为 0"只是断言；有流水，它是可以用一个 SQL（`group by book_id` 求和比对）反复证明的守恒律（业务约束 B7）。
2. **`biz_no` 提供最后一道幂等防线**：`idempotent_record` 保证接口级幂等，`uk(biz_no)` 保证**业务流水级**幂等——即使幂等表被清理或绕过（如补偿任务重跑、定时任务重入），重复扣减也会被唯一键拦住。两道防线层级不同，互补而非冗余。
3. **`trace_id` 让扣减可反查全链路**：从一条流水可以直接跳转到该次请求的 Java 日志与 Agent 会话，把"业务现象（这本书少了 1 册）"与"技术现场（哪次请求、哪个工具、哪条 SQL）"连起来——这正是 P3 排障效率的关键。
4. **`before_qty`/`after_qty` 使对账无需回放**：每次变更都自带前后快照，可逐条校验单调性与连续性，也能直接定位是**哪一条**流水把库存改错的，而不只是一个总数不平。
5. **支撑 S5/S6 的最终一致方案**：蓝图 §6.1 的 S5（Redis 预扣 + 异步落库）必须有"预扣日志 + 补偿"才能收敛；`stock_ledger` 就是这套补偿的标准依据，也让"最终一致"的边界可审计。
6. **满足上线与运维要求**：M4/M6 阶段的故障复盘、对账演练、告警规则（`smartlib_stock_ledger_unbalanced_total` 必须恒为 0）都依赖它（02 概设 §14 风险 K4、评审意见 11）。

**被否决的替代方案及否决原因**

| 方案 | 否决原因 |
|---|---|
| 不写流水，只靠 `book_stock` 当前值 | 只能看到结果、无法回答原因；超卖一旦发生无从定位；"零超卖"无法被独立验证 |
| 只写应用日志（文件/ELK） | 日志非事务性、会丢、可被清理、不可用 SQL 对账；无法参与"守恒校验"这类结构化断言 |
| 只依赖 `uk(biz_no)` 而不做对账 | 唯一键只能防重复，不能发现"该扣没扣""扣多了""before/after 不连续"等错误 |
| 用 binlog/CDC 事后重建流水 | 只能事后、无法在事务边界内保证，且需要额外中间件（违反零依赖约束）；适合归档不适合审计主口径 |
| 只记录"变更总数"（不分 op_type/不记前后值） | 粒度不足，无法区分业务原因，也无法逐条定位错误来源 |

**正面后果**
- "零超卖"从承诺变成可反复执行的守恒校验，可写进测试报告与运维巡检。
- 业务级幂等有了独立防线，补偿任务与定时任务可安全重跑。
- `trace_id` 打通"业务现象 → 技术现场"，显著缩短排障时间（对症 P3 的定位诉求）。

**负面后果与缓解**
- 负面：① 每次库存变更多一次 `insert`，写路径变长、热点书上写放大；② 表持续增长（3 年约 576 万行 / 约 1.5GB 估算）；③ 引入"流水与库存不一致"这一新的失败模式，需要监控与处置流程。
- 缓解：① 流水写入与库存更新**同事务**，避免额外的分布式事务开销，且 `insert` 为窄表顺序写，开销远小于扣减本身；② 按 `created_at` 分区/归档（保留近 12 个月在线，其余归档到冷存储），保留 `idx(book_id, created_at)` 使按书查询依然高效；③ 对账任务失败处置流程写明：告警（`unbalanced_total > 0` 立即 P1）→ 冻结该书借还 → 按 `stock_ledger` 逐条回放定位 → 人工工单模板（02 概设 §15 评审意见 11）；④ 用 `sum(delta)` 与库存变化量的定期校验代替实时校验，避免每次写都做聚合查询。

**相关代码位置**
- `backend/src/main/java/com/smartlib/domain/entity/StockLedger.java`
- `backend/src/main/java/com/smartlib/domain/enums/StockOpType.java`
- `backend/src/main/java/com/smartlib/service/stock/StockStrategy.java`（实现类内写流水）
- `backend/src/main/java/com/smartlib/mapper/StockLedgerMapper.java`
- `deploy/sql/01-schema.sql`：`stock_ledger` 表、`uk(biz_no)`、`idx(book_id, created_at)`
- `backend/src/test/java/com/smartlib/BorrowConcurrencyTest.java`（断言成功借出数 == 10 且流水守恒）
- `backend/src/main/java/com/smartlib/web/AdminController.java`（对账触发入口）
- `deploy/prometheus/alert-rules.yml`：`smartlib_stock_ledger_unbalanced_total > 0` P1 告警

---

## 附录 A：ADR 与蓝图解决方案矩阵的映射

| 蓝图矩阵 | 相关 ADR | 说明 |
|---|---|---|
| §6.1 P1 库存扣减 S1~S6 | ADR-004、ADR-007、ADR-012 | 默认 S2；分表支撑缓存/锁隔离；流水提供审计与守恒 |
| §6.2 P2 检索 Q1~Q5 | ADR-006、ADR-002 | 默认 Q3；SLI 依赖 SQL 可控性做基线对比 |
| §6.3 P3 高可用 A1~A6 | ADR-005、ADR-009、ADR-001 | 自研限流熔断 + 幂等落库 + Actuator 探针 |
| §6.4 P4 Agent G1~G5 | ADR-010、ADR-011 | 自研 ReAct/Plan-Execute；MockLLM 保障离线与确定性 |
| §6.5 P5 缓存与一致性 | ADR-007、ADR-009 | 库存不作缓存事实源；幂等不依赖可选组件 |

## 附录 B：变更与新增流程

1. 任何影响版本号、类名、接口路径、表名的变更，**必须先改蓝图**（[`docs/00-项目蓝图.md`](./00-项目蓝图.md)）并新增 ADR，再改代码。
2. 新增 ADR 编号连续递增（下一条为 **ADR-013**），格式与本文档完全一致。
3. 被取代的 ADR 不改正文，只在状态栏改为 `已废弃` 并注明"被 ADR-0xx 取代"。
4. ADR 中的"相关代码位置"必须在实现完成后回填为真实路径，禁止保留占位符。

## 附录 C：版本记录

| 版本 | 日期 | 变更 | 备注 |
|---|---|---|---|
| v1.0.0 | 2026-10-16 | 首次发布 ADR-001 ~ ADR-012 | 覆盖技术栈、持久层、库存、检索、韧性、双环境、幂等、Agent、审计共 12 项决策 |

> 注：蓝图 §9 文档矩阵中 ADR 条目原写作「ADR-001~ADR-010」，本文档按概设评审要求扩展至 ADR-012。**该差异已随蓝图 v1.1.0 关闭**（蓝图 §9 已同步更新为 `ADR-001~ADR-012`）。
