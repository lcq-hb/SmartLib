# SmartLib 基准测试汇总

> 本文件由 `scripts/bench/run_all.ps1` 自动生成，请勿手工编辑。
> 所有数字都是**本机实测**，没有目标值/承诺值；不同机器、不同数据量不可直接横向比较。

## 本次测量的有效性前提（先看这里，再看数字）

1. **每轮库存独立且已知**：每种策略的每一轮开始前，目标书的 `availableCopies` 都是已知值；
   每轮借出后立即用 `POST /api/borrows/{id}/return` 把本轮借出的复本全部归还，
   使库存回到该轮初始值。逐轮证据见「逐轮明细」表（availableBefore / afterBorrow / afterReturn）。
   只有满足这一点，六种策略的 QPS/P99 才是**可比的**。
2. **读者池是干净的**：所有参与压测的读者都满足 `creditScore >= min_credit_score`、
   无逾期、无欠费、`status=ACTIVE`（筛选过程与排除原因见下文）。
   池子里混入不合格读者会引入每轮都一样的固定失败，把"策略差异"淹没成"规则拒绝"。
3. **每轮成功数应当恰好等于该轮可用复本数**：干净读者池 + 每个读者只借 1 次
   （`--reader-mode distinct`）+ 请求数 = 复本数，且目标书的复本数 <= 干净读者数
   （否则读者不够，借不满）。**成功数少了 = 误拒**（该成功的被拒了）；
   **多了 = 超卖**。这是本表最有说服力的一列。
   六种策略**压同一本目标书**（书名见参数行），每轮都借空再还回去 ——
   压同一本书不是缺陷：每轮都从同一个已知库存出发，样本量与数据分布对六种策略完全一致。
4. **QPS 是下界，不是服务端极限**：`load_borrow.py` 用纯标准库 `http.client`，
   每个请求新建一条 TCP 连接（没有连接池复用），客户端自身（GIL + 线程）也是瓶颈。
   因此这些 QPS 只能当作"在同一客户端条件下的相对比较"，不能当容量规划依据。
5. **样本量**：每策略 = 轮数 × 每轮复本数（见参数行）。样本只有几十次时
   P99/QPS 抖动明显，不要拿小数点后几位的差异下结论。

- 生成时间：2026-10-16 21:46:02
- 目标后端：http://127.0.0.1:8080
- 每策略轮数：5 / 每轮复本数（=每轮请求数）：8
- 每策略样本量：40 次借阅请求（另有等量归还请求）
- 读者池：9 人 [1, 3, 4, 6, 7, 8, 9, 10, 11]（已排除 3 人）
- Python：E:\LenovoSoftstore\Install\python.exe
- 后端健康检查：overall=UP, db=UP, http=200
- 管理端点：可用（可运行期切换策略/引擎）
- 收尾对账：PASS
- 规则阈值：min_credit_score=60，fine_free_limit=10
- 库存判据来源：内部签名接口 `GET /internal/agent/tools/books/{id}/stock`（直读库）

## 读者池筛选（1..20，阈值 creditScore>=60、无逾期、无欠费、ACTIVE）

### 参与压测的读者（9 人）

| readerId | creditScore | status |
|---|---|---|
| 1 | 100 | ACTIVE |
| 3 | 100 | ACTIVE |
| 4 | 100 | ACTIVE |
| 6 | 100 | ACTIVE |
| 7 | 100 | ACTIVE |
| 8 | 100 | ACTIVE |
| 9 | 100 | ACTIVE |
| 10 | 100 | ACTIVE |
| 11 | 100 | ACTIVE |

### 被排除的读者（3 人）

| readerId | 排除原因 | 证据 |
|---|---|---|
| 2 | 存在逾期（credit.overdueCount=1，loans 逾期条数=1） | creditScore=95 status=ACTIVE overdueCount=1 unpaidFine=8.00 |
| 5 | 信用分 55 < 阈值 60 | creditScore=55 status=ACTIVE overdueCount=0 unpaidFine=0 |
| 12 | status=FROZEN（非 ACTIVE，最常见是 FROZEN） | creditScore=100 status=FROZEN overdueCount=0 unpaidFine=0 |

> 为什么要写这张表：被排除的读者正是【信用分不足/有逾期/有欠费】这些业务规则的触发者。
> 如果把他们放进读者池，每个策略都会收到同样的失败，表格看起来【六种策略都很差】——
> 那是抽样污染，不是策略问题。

## 表 1：六种库存扣减策略（每轮独立库存、独立归还）

| 方案 | 书目id | 轮数 | 每轮复本 | 期望成功 | 实测成功 | 超卖 | 错误率 | QPS | P50(ms) | P90(ms) | P99(ms) | 扣减守恒 | 库存复原 | 结果文件 | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| local-lock | 21 | 5 | 8 | 40 | 40 | 0 | 0.00% | 89.14 | 4.738 | 6.465 | 17.706 | 是 | 是 | borrow_local-lock.json | 切换=OK(current=local-lock)；目标书 21 / 每轮 8 复本；缓存偏旧轮次=5；消息=ok |
| optimistic-db | 21 | 5 | 8 | 40 | 40 | 0 | 0.00% | 93.20 | 5.174 | 7.453 | 18.791 | 是 | 是 | borrow_optimistic-db.json | 切换=OK(current=optimistic-db)；目标书 21 / 每轮 8 复本；缓存偏旧轮次=5；消息=ok |
| pessimistic-db | 21 | 5 | 8 | 40 | 40 | 0 | 0.00% | 84.51 | 4.796 | 9.607 | 15.796 | 是 | 是 | borrow_pessimistic-db.json | 切换=OK(current=pessimistic-db)；目标书 21 / 每轮 8 复本；缓存偏旧轮次=5；消息=ok |
| version-retry | 21 | 5 | 8 | 40 | 40 | 0 | 0.00% | 74.81 | 5.166 | 7.978 | 18.754 | 是 | 是 | borrow_version-retry.json | 切换=OK(current=version-retry)；目标书 21 / 每轮 8 复本；缓存偏旧轮次=5；消息=ok |
| redis-lua | 21 | 5 | 8 | 40 | 40 | 0 | 0.00% | 99.27 | 5.626 | 10.363 | 16.368 | 是 | 是 | borrow_redis-lua.json | 切换=OK(current=redis-lua)；目标书 21 / 每轮 8 复本；缓存偏旧轮次=5；消息=ok |
| mq-serial | 21 | 5 | 8 | 40 | 40 | 0 | 0.00% | 114.82 | 3.687 | 5.068 | 11.413 | 是 | 是 | borrow_mq-serial.json | 切换=OK(current=mq-serial)；目标书 21 / 每轮 8 复本；缓存偏旧轮次=5；消息=ok |

判据解读：
- **期望成功 = 实测成功** ⇒ 既没有超卖，也没有误拒（该借到的都借到了）。
- **超卖 = 0**：每轮借出后 `availableCopies >= 0`。
- **扣减守恒 = 是**：每轮 `扣减后可借 == 扣减前可借 - 成功数`（成功 n 次就恰好扣 n 册）。
- **库存复原 = 是**：每轮归还后 `availableCopies` 回到该轮初始值（这是【下一轮可比】的前提）。

## 表 2：逐轮明细（【每轮独立库存】的直接证据）

| 方案 | 轮 | 书目id | 借前可借 | 借后可借 | 归还后可借 | 缓存读数 | 期望成功 | 实测成功 | 匹配 | 扣减守恒 | 库存复原 | 归还笔数 | 超卖 | 该轮P99(ms) | 状态分布 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| local-lock | 1 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 5.493 | {"200 (code=0)":8} |
| local-lock | 2 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 6.840 | {"200 (code=0)":8} |
| local-lock | 3 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 18.238 | {"200 (code=0)":8} |
| local-lock | 4 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 14.692 | {"200 (code=0)":8} |
| local-lock | 5 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 5.792 | {"200 (code=0)":8} |
| optimistic-db | 1 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 8.610 | {"200 (code=0)":8} |
| optimistic-db | 2 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 7.148 | {"200 (code=0)":8} |
| optimistic-db | 3 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 14.873 | {"200 (code=0)":8} |
| optimistic-db | 4 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 7.115 | {"200 (code=0)":8} |
| optimistic-db | 5 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 19.685 | {"200 (code=0)":8} |
| pessimistic-db | 1 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 14.181 | {"200 (code=0)":8} |
| pessimistic-db | 2 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 15.752 | {"200 (code=0)":8} |
| pessimistic-db | 3 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 14.081 | {"200 (code=0)":8} |
| pessimistic-db | 4 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 10.479 | {"200 (code=0)":8} |
| pessimistic-db | 5 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 6.573 | {"200 (code=0)":8} |
| version-retry | 1 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 19.890 | {"200 (code=0)":8} |
| version-retry | 2 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 15.021 | {"200 (code=0)":8} |
| version-retry | 3 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 7.533 | {"200 (code=0)":8} |
| version-retry | 4 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 12.703 | {"200 (code=0)":8} |
| version-retry | 5 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 7.893 | {"200 (code=0)":8} |
| redis-lua | 1 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 12.290 | {"200 (code=0)":8} |
| redis-lua | 2 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 10.619 | {"200 (code=0)":8} |
| redis-lua | 3 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 6.661 | {"200 (code=0)":8} |
| redis-lua | 4 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 17.949 | {"200 (code=0)":8} |
| redis-lua | 5 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 6.087 | {"200 (code=0)":8} |
| mq-serial | 1 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 5.094 | {"200 (code=0)":8} |
| mq-serial | 2 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 14.591 | {"200 (code=0)":8} |
| mq-serial | 3 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 4.345 | {"200 (code=0)":8} |
| mq-serial | 4 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 3.770 | {"200 (code=0)":8} |
| mq-serial | 5 | 21 | 8 | 0 | 8 | 8 | 8 | 8 | True | True | True | 8 | False | 5.228 | {"200 (code=0)":8} |

> 「缓存读数」= 同一时刻 `GET /api/books/{id}` 返回的 availableCopies。
> 它与「借后可借」不一致时，说明**书目详情缓存没有随库存变化失效**（见文末【已知问题】）。

## 表 3：检索引擎（Q1 like / Q3 inverted-index，运行期真实切换）

| 请求引擎 | 实际引擎 | 请求数 | QPS | P50(ms) | P90(ms) | P99(ms) | 错误率 | 空结果数 | 备注 |
|---|---|---|---|---|---|---|---|---|---|
| like | like | 200 | 2614.56 | 1.973 | 2.777 | 3.983 | 0.00% | 25 |  |
| inverted-index | inverted-index | 200 | 2782.19 | 1.807 | 2.639 | 3.844 | 0.00% | 0 | 索引重建 2.34 ms |

> 「请求引擎」是脚本要求切换到的引擎，「实际引擎」是从 `/api/admin/search-engine` 读回的服务端真实引擎。
> 两列不一致时以「实际引擎」为准（否则就是编造对比数据）。

## 表 4：召回率（两个引擎各跑一遍同一份人工标注集）

| 引擎 | 实际引擎 | 评估查询数 | 平均召回率(%) | Top1 命中率(%) | 结果文件 | 口径 |
|---|---|---|---|---|---|---|
| like | like | 42 | 64.29 | 64.29 | recall_like.json | 口径：命中期望 id 的比例（人工小标注集，属可用性召回） |
| inverted-index | inverted-index | 42 | 100.00 | 100.00 | recall_inverted-index.json | 口径：命中期望 id 的比例（人工小标注集，属可用性召回） |

**两个概念必须分清**（表头列名已分开）：
- **平均召回率** = 对每条标注查询 `|命中期望 id| / |期望 id|` 再取平均，衡量「找全了没有」。
- **Top1 命中率** = 期望 id 出现在**第一条**结果里的查询占比，衡量「排第一的对不对」。
- 两者都基于 `scripts/bench/recall_dataset.json`，期望集是人工标注的
  「至少应被召回」的**小集合**（id 取自 `backend/.../db/data.sql` 里真实存在的 24 本书），
  不是完整相关文档集合，因此属**可用性召回**，不能当学术意义的 recall 用。
- `expected_book_ids` 为空的条目（负样本）不计入分母，会记在 JSON 的 `recall.skippedEntries` 里。

## 库存对账（按阶段记录，`GET /api/admin/reconcile`）

| 阶段 | 结论 | violations | redis-lua 降级次数（累计值） |
|---|---|---|---|
| 策略 local-lock 之后 | PASS | （空） | 120 |
| 策略 optimistic-db 之后 | PASS | （空） | 120 |
| 策略 pessimistic-db 之后 | PASS | （空） | 120 |
| 策略 version-retry 之后 | PASS | （空） | 120 |
| 策略 redis-lua 之后 | PASS | （空） | 160 |
| 策略 mq-serial 之后 | PASS | （空） | 160 |
| 全部压测结束 | PASS | （空） | 160 |

判据：`data.consistent == true` 且 `data.violations` 为空数组，表示库存与 `stock_ledger` 守恒、
且没有负库存。任何一行 FAIL 都是 P0 级问题（说明有路径绕过了 `StockService` 的扣减入口）。

`redisLuaDegradedCount` 是**自后端启动起的累计值**（不是本阶段增量）；
相邻两行相减就是该阶段新增的降级次数。它大于 0 说明本机 Redis 不可用、
S5（redis-lua）实际走的是降级路径（等价于 S2 的 DB 条件更新）——
所以**不能**把表 1 里 redis-lua 那一行当作「Lua 原子预扣」的性能证据。

收尾响应原文：

```text
{"code":0,"msg":"ok","data":{"violations":[],"violationCount":0,"consistent":true,"redisLuaDegradedCount":160,"redisLuaSuccessCount":0},"traceId":"328b491e05244123","success":true}
```

## 原始数据与复现方式

`scripts/bench/results/*.json` 是**本地运行产物，不入库**（`.gitignore` 忽略了 `*.json`）。
因此上表的数字**不能靠打开 JSON 复核**，只能靠重新跑一遍复现；
本文件（summary.md）是随仓库提交的**唯一证据快照**。

复现命令（在仓库根目录）：

```powershell
# 一键复现本文件（需要后端已在 http://127.0.0.1:8080 运行，且 smartlib.admin.enabled=true）
powershell -ExecutionPolicy Bypass -File scripts/bench/run_all.ps1 `
    -BaseUrl http://127.0.0.1:8080 -CopiesPerRound 8 -Cycles 5
```

单策略单跑（换任意策略名即可）：

```powershell
python scripts/bench/load_borrow.py --url http://127.0.0.1:8080 --book-id 21 `
    --threads 8 --requests 8 --cycles 5 `
    --return-each-cycle --reader-mode distinct --reader-ids 1,3,4,6,7,8,9,10,11 `
    --label manual --out scripts/bench/results/borrow_manual.json
```

本次运行写出的文件（未入库，仅供本机核查）：

- `borrow_local-lock.json` —— 策略 local-lock
- `borrow_optimistic-db.json` —— 策略 optimistic-db
- `borrow_pessimistic-db.json` —— 策略 pessimistic-db
- `borrow_version-retry.json` —— 策略 version-retry
- `borrow_redis-lua.json` —— 策略 redis-lua
- `borrow_mq-serial.json` —— 策略 mq-serial
- `search_like.json` —— 检索 like
- `search_inverted-index.json` —— 检索 inverted-index
- `recall_like.json` —— 召回 like
- `recall_inverted-index.json` —— 召回 inverted-index
- `reconcile.json` —— 收尾对账原始响应
- `notes.txt` —— 跳过的步骤与失败原因（若本次全部成功则不存在）

## 已知问题与限制（必须和数字一起看）

### 1) 【后端缺陷·已实测确认】书目详情缓存不随库存变化失效

现象：借出 8 册后立刻 `GET /api/books/{id}`，`availableCopies` 仍返回旧值；
同时内部签名接口 `GET /internal/agent/tools/books/{id}/stock` 返回正确值（0）。
原因：`BookService.getById` 走 `CacheGuard`（多级缓存），而后端**唯一**的
`StockService.StockChangeListener` 实现是 `InvertedIndexSearchEngine`（只更新倒排索引），
**没有任何代码在库存变化时失效书目详情缓存**，只能等 TTL 到期自愈。
影响：读者/前端看到的「可借数」会短暂偏大（本例实测每一轮都偏旧）；
**不影响扣减正确性**（`book_stock` 与 `stock_ledger` 守恒，对账全 PASS）。
本表的处理：库存判据全部改用内部签名接口（权威），并把缓存读数单独列出来做对照。
修复建议（不在本次写入范围，需后端同学决定）：让书目的写路径同时失效
`smartlib:book:{id}` 缓存，或给 BookView 的库存字段设置更短的 TTL。

### 2) 客户端即瓶颈

压测客户端用纯标准库 `http.client`，每请求新建 TCP 连接、无连接池复用，
且受 Python GIL 限制。所以 QPS 是**下界**，只能用于「同一客户端条件下的相对比较」。
要测服务端上限请用 wrk/JMeter，并对比两者差异。

### 3) S5（redis-lua）与 S6（mq-serial）的前提

- S5 依赖 Redis 可用。Redis 不可用时后端会自动降级为 S2（`DegradeFallback`），
  此时 S5 那一行的数字实际上是 S2 的。每轮对账里的 `redisLuaDegradedCount`
  就是降级的累计次数：它 > 0 时**不能**把该行当作「Lua 原子预扣」的性能。
- S6 在本地是「单线程 Dispatcher + 有界队列」的模拟实现，不是真实 RocketMQ 事务消息。

### 4) 压测会留下数据痕迹（但不改库存）

每轮借出后都归还，因此 `book_stock.available_copies` 回到原值；
但 `borrow_record`（`RETURNED`）与 `stock_ledger`（`DEDUCT`+`RELEASE`）会新增记录 ——
这是对账仍然守恒的原因。需要干净数据请重建库（本地 H2 重启即重建）。

### 5) 检索引擎与召回率的对比边界

- 引擎切换是运行期生效的（`PUT /api/admin/search-engine`），但切换后必须 `--reindex`；
  本次每轮切换后都重建了倒排索引（见检索表备注里的毫秒数）。
- 本次数据量是演示库（24 种书目），**不能**用来论证「120 万书目下的 P99」。
  要出那种结论，先用 `scripts/gen_books.py` 灌 20 万条再跑，并在文档里写明数据量。
- 召回率用的是 42 条人工小标注集，属可用性召回，不是完整相关集。

### 6) 没有跑起来的项不要下结论

表里出现 `-` 表示该项本轮未测得，不等价于「性能为 0」。
如果管理端点不可用（`smartlib.admin.enabled` 未开），策略/引擎切换会失败，
此时表里的「实际引擎/实际策略」列会暴露真相，不要照抄「请求」列。
