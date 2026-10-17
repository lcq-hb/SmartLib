# SmartLib 部署与基准测试基础设施

> 本目录只负责**部署**与**压测**，不含业务代码。
> 唯一技术契约是 [`docs/00-项目蓝图.md`](../docs/00-项目蓝图.md)：§3 技术栈、§4 架构、§6.1~6.3 方案矩阵、§7 SLO、§8 接口契约。
> 本文件是部署目录的唯一说明文档（`docs/` 下的文档由各自负责人维护，此处不复制）。

---

## 1. 目录结构与文件用途

```
deploy/
├── docker-compose.yml                 Compose 编排：mysql + redis + backend×2 + agent×2 + nginx
├── Dockerfile.backend                 Spring Boot 4.1.1 / Java 21 多阶段镜像（JRE 运行、非 root）
├── Dockerfile.agent                   FastAPI / Python 3.12 多阶段镜像（tini + 非 root）
├── nginx/
│   └── nginx.conf                     Nginx 入口配置（http 上下文片段，挂到 conf.d）
├── sql/
│   ├── 01-schema.sql                  表结构（与 backend/src/main/resources/db/schema.sql 逐条一致）
│   ├── 02-seed.sql                    初始化数据（与 backend/.../db/data.sql 逐条一致）
│   ├── mysql-production-extras.sql    生产增量优化：ngram 全文索引(Q4)/分区/账号权限/慢查询
│   ├── README.md                      SQL 目录约定（漂移规则、可移植语法子集、命令）
│   └── generated/                     gen_books.py 的输出目录（已 gitignore，含 .gitkeep）
├── k8s/                               Kubernetes 清单（kustomize 组织）
│   ├── namespace.yaml                 smartlib 命名空间（Pod Security: restricted）
│   ├── configmap.yaml                 非敏感配置（主机名/库名/策略名/开关）
│   ├── secret.yaml.example            Secret 模板（真实 secret.yaml 已被 gitignore）
│   ├── backend-deployment.yaml        2 副本 + 滚动更新(maxSurge=1/maxUnavailable=0) + 三类探针 + 优雅停机
│   ├── backend-service.yaml           ClusterIP，无会话粘性（后端无状态）
│   ├── agent-deployment.yaml          2 副本，探针只打 /health，停机宽限 60s
│   ├── agent-service.yaml             ClusterIP
│   ├── mysql-statefulset.yaml         StatefulSet + headless Service + PVC + init ConfigMap
│   ├── redis-deployment.yaml          Deployment(Recreate) + Service + PVC（AOF）
│   ├── ingress.yaml                   主 Ingress + SSE 专用 Ingress（注意 path 冲突说明）
│   ├── hpa.yaml                       HPA：后端 CPU 70%（2~10），Agent CPU 75%（2~6）
│   └── kustomization.yaml             统一 namespace/labels/images
└── README.md                          本文件

scripts/
├── gen_books.py                       纯标准库书目数据生成器（SQL / CSV，可复现）
├── start-local.ps1                    本地零依赖启动（H2 + MockLLM）
└── bench/
    ├── load_borrow.py                 借阅接口并发压测 + 超卖检查
    ├── search_bench.py                检索接口压测 + 召回率评估 + 运行期切引擎
    ├── recall_dataset.json            42 条中文查询的人工标注集（覆盖 data.sql 全部 24 本书）
    ├── run_all.ps1                    一键基准：六策略 + 两引擎 + 召回率 + 对账 → summary.md
    └── results/                       结果目录（已 gitignore，含 .gitkeep）
```

根目录还有：`.gitignore`、`.editorconfig`、`Makefile`、`.github/workflows/ci.yml`。

---

## 2. 本地零依赖启动（推荐先跑这个）

不需要 Docker、不需要 MySQL/Redis，用 H2 内存库 + MockLLM 就能把两个服务跑起来：

```powershell
# 【必须】用 -ExecutionPolicy Bypass，直接 & 脚本会被执行策略拦截
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1

# 只起后端（H2 内存库 + 管理端点打开，便于跑压测）
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1 -SkipAgent

# 只起 Agent
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1 -SkipBackend
```

脚本做的事：
1. `scripts/mvn.ps1 -f backend/pom.xml spring-boot:run -Dspring-boot.run.profiles=local`
   （用仓库自带的 Maven 启动器，绕开 `mvn.cmd` 在中文路径下的代码页问题）
   并追加 `--smartlib.admin.enabled=true`，否则 `/api/admin/**` 全部 404，压测无法切策略/引擎。
2. 若 `agent-service/app/main.py` 存在，则建 `.venv`、装 `requirements.txt`，
   用 `LLM_PROVIDER=mock` 起 uvicorn；不存在则**明确跳过并给出提示**（不报错退出）。
3. 轮询 `/actuator/health` 与 `/health`，最后打印访问地址、可直接复制的验证命令、停止方式。

验证命令（脚本会打印同样的内容）：

```powershell
# 健康
Invoke-RestMethod http://127.0.0.1:8080/actuator/health
# 检索（中文查询）
Invoke-RestMethod 'http://127.0.0.1:8080/api/books?query=Java&page=1&size=5&sort=relevance'
# 借书（Idempotency-Key 必填）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/api/borrows `
  -Headers @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() } `
  -ContentType 'application/json' -Body '{"readerId":1,"bookId":1}'
# 运行期切库存策略（需要 smartlib.admin.enabled=true）
Invoke-RestMethod -Method Put 'http://127.0.0.1:8080/api/admin/stock-strategy?name=optimistic-db'
# 对账（data.consistent=true 且 violations 为空 == 健康）
Invoke-RestMethod http://127.0.0.1:8080/api/admin/reconcile
```

---

## 3. 基准测试

### 3.1 一键跑全部（六种库存策略 + 两种检索引擎 + 召回率 + 对账）

```powershell
# 后端已在 8080 跑起来后
powershell -ExecutionPolicy Bypass -File scripts/bench/run_all.ps1 `
    -BaseUrl http://localhost:8080 -CopiesPerRound 8 -Cycles 5
```

参数含义：`-CopiesPerRound` = 每轮借出多少册（也就是每轮请求数），`-Cycles` = 每种策略跑多少轮；
每策略样本量 = `CopiesPerRound × Cycles`。默认值在演示库（24 书目、最多 12 复本）上跑得比较合适。

产物（都在 `scripts/bench/results/`）：

| 文件 | 内容 | 是否入库 |
|---|---|---|
| `summary.md` | 汇总报告：有效性前提 + 读者池筛选 + 六策略表 + **逐轮明细** + 检索表 + 召回率表 + 逐阶段对账 + 复现说明 + 已知问题 | ✅ 入库（唯一证据快照） |
| `borrow_<strategy>.json` | 六种策略各自的原始压测数据（含逐轮 stockBefore/After/Return） | ❌ `*.json` 被 gitignore |
| `search_like.json` / `search_inverted-index.json` | 两个引擎的检索压测数据（含实测引擎名） | ❌ |
| `recall_like.json` / `recall_inverted-index.json` | 两个引擎的召回率明细（逐条 query 命中情况） | ❌ |
| `reconcile.json` | 收尾对账原始响应 | ❌ |
| `notes.txt` | 跳过的步骤与失败原因（如管理端点未开启） | ❌ |

> **`结果文件` 列怎么用**：因为 `*.json` 不入库，表里那一列**不能**用来"打开文件核对数字"，
> 它只是本次运行的产物名。要复核只能**重跑**——`summary.md` 里给了完整复现命令。
> 换句话说：`summary.md` 是随仓库走的证据，JSON 是本机中间产物。

`summary.md` 只写**实测值**，不写目标值；数字旁边永远跟着口径与限制说明。

### 3.2 单独压测某个方案

```powershell
# 借阅压测：并发抢同一本书 + 压测后校验 availableCopies 是否 < 0（超卖检查）
python scripts/bench/load_borrow.py --url http://localhost:8080 `
    --book-id 5 --threads 200 --requests 200 --reader-ids 1-30 `
    --label pessimistic-db --out scripts/bench/results/borrow_pessimistic-db.json

# 检索压测：真的切换引擎（--switch-engine），倒排索引还要 --reindex
python scripts/bench/search_bench.py --url http://localhost:8080 `
    --engine inverted-index --switch-engine --reindex `
    --threads 20 --requests 400 --label Q3-inverted `
    --out scripts/bench/results/search_inverted-index.json

# 召回率评估（数据集既当查询集，也当标注集）
python scripts/bench/search_bench.py --url http://localhost:8080 `
    --engine inverted-index --switch-engine `
    --queries-file scripts/bench/recall_dataset.json `
    --recall-check scripts/bench/recall_dataset.json `
    --out scripts/bench/results/recall_inverted-index.json
```

### 3.3 测量设计：为什么每轮都要重置库存（v2 的关键改动）

`run_all.ps1` 的第一版有一个**测量设计缺陷**：六种策略依次压**同一本书**，第一轮就把复本借光了，
后面几轮的失败原因是「库存没了 / 同一读者重复借」——于是那张「六策略对比表」里的
QPS/P99 测的其实是**业务规则拒绝的速度**，策略之间根本不可比。v2 的修法：

| 问题 | v2 的做法 |
|---|---|
| 各轮共享库存 | **每轮回借出后立即归还**：`POST /api/borrows/{id}/return`（纯公开 API，不改后端），库存回到该轮初始值。逐轮证据写入 `summary.md` 表 2 的 `借前/借后/归还后` 三列 |
| 读者池被污染 | 预筛读者：`GET /api/readers/{id}/credit` + `/loans`，排除信用分 < `min_credit_score`、有逾期、有欠费、非 ACTIVE 的读者；筛选结果与排除原因都写进 `summary.md` |
| 期望值说不清 | 每轮期望成功数 = `min(该轮可借复本数, 本轮请求数, 干净读者数)`；目标书选「复本数 ≤ 干净读者数」的书，这样每轮能**恰好借空**：成功数少了 = 误拒，多了 = 超卖 |
| 库存判据读缓存 | 一律读**权威源** `GET /internal/agent/tools/books/{id}/stock`（HMAC 签名，直读库）。原因见第 8 节已知问题 15：`GET /api/books/{id}` 的缓存不会随库存变化失效 |
| 只给一条全局对账 | 每种策略跑完都调一次 `GET /api/admin/reconcile`，逐阶段记录结论与 violations |

`load_borrow.py` 的对应参数：`--cycles N`、`--return-each-cycle`、`--reader-mode distinct`。
不加 `--return-each-cycle` 时，多轮会被 CLI 直接拒绝（必须显式 `--allow-unreset-stock`），
避免"不小心跑出不可比数据"。

退出码约定（便于脚本判定）：

| 退出码 | 含义 |
|---|---|
| 0 | 正常完成 |
| 2 | 参数错误（含"多轮但没开 `--return-each-cycle`"） |
| 3 | **发生超卖**或**扣减不守恒**（`availableCopies < 0`，或 `扣减后可借 ≠ 扣减前可借 - 成功数`） |
| 4 | 全部请求失败（后端没起来 / 地址写错） |
| 5 | 期望成功数与实测不一致，或某轮结束后库存没复原 |

### 3.4 召回率口径（必须和数字一起看）

- 数据集每条是 `{"query": "...", "expected_book_ids": [1, 2]}`，期望 id 取自
  `backend/src/main/resources/db/data.sql` 里**真实存在**的 24 本书（覆盖全部 24 本、共 42 条查询）。
- **两个指标必须分清**（`summary.md` 表 4 已拆成两列，不要混着念）：
  - **平均召回率** = 对每条查询 `|命中期望 id| / |期望 id|` 再取平均 → 衡量「找全了没有」；
  - **Top1 命中率** = 期望 id 出现在**第一条**结果里的查询占比 → 衡量「排第一的对不对」。
- 期望集是「至少应被召回」的人工**小集合**，不是完整相关文档集合 → 属**可用性召回**，
  不能当学术意义的 recall 用。这句话在 `search_bench.py` 的 `evaluate_recall` docstring、
  `summary.md` 与本节里是**同一口径**。
- `expected_book_ids` 为空的条目（负样本）**不计入分母**，会在 JSON 的
  `recall.skippedEntries` 里体现，避免人为拉低/拉高召回率。

---

## 4. Docker Compose 起生产形态

```bash
cd deploy
docker compose up -d --build          # 首次构建镜像 + 起 7 个服务
docker compose ps                     # 看健康状态
docker compose logs -f backend-1
docker compose down                   # 停容器，保留数据卷
docker compose down -v                # 连数据卷一起删（下次启动会重跑 initdb 脚本）
```

启动后的入口：

| 地址 | 说明 |
|---|---|
| `http://localhost/` | Nginx 网关首页 |
| `http://localhost/actuator/health` | 后端健康（经 Nginx 负载到两个实例） |
| `http://localhost/agent-health` | Agent 健康 |
| `http://localhost/agent/chat/sync` | Agent 同步对话（SSE 走 `/agent/`，已关 buffering） |
| `http://localhost:8081`、`http://localhost:8082` | 后端实例直连（排障用，压测单实例用这个） |
| `http://localhost:13306`（默认 3306） | MySQL |

服务与端口对照（容器内 → 宿主）：

| 服务 | 镜像 | 容器端口 | 宿主端口（可用 .env 覆盖） |
|---|---|---|---|
| mysql | `mysql:8.0` | 3306 | 3306（`MYSQL_PORT_HOST`） |
| redis | `redis:7-alpine` | 6379 | 6379（`REDIS_PORT_HOST`） |
| backend-1 | 自建 `smartlib/backend:1.0.0` | 8080 | 8081（`BACKEND1_PORT`） |
| backend-2 | 同上镜像 | 8080 | 8082（`BACKEND2_PORT`） |
| agent-1 | 自建 `smartlib/agent:1.0.0` | 8000 | 8001（`AGENT1_PORT`） |
| agent-2 | 同上镜像 | 8000 | 8002（`AGENT2_PORT`） |
| nginx | `nginx:1.27-alpine` | 80 | 80（`NGINX_PORT`） |

常用环境变量（Compose 会自动读 `deploy/.env`，该文件已被 gitignore）：

```ini
MYSQL_ROOT_PASSWORD=root
MYSQL_USER=smartlib
MYSQL_PASSWORD=smartlib
# 策略与引擎的值必须与 StockStrategy.name() / SearchEngine.name() 逐字一致（短横线）：
#   库存策略 optimistic-db | pessimistic-db | version-retry | local-lock | redis-lua | mq-serial
#   检索引擎 like | inverted-index | elasticsearch
# 写成 optimistic_db / inverted_index 不会报错，只会静默退回默认值
# —— 这类"变量名/取值写错但不报错"的问题由 scripts/tools/check_env_names.py 在 CI 里守住。
SMARTLIB_STOCK_STRATEGY=optimistic-db
SMARTLIB_SEARCH_ENGINE=inverted-index
SMARTLIB_ADMIN_ENABLED=true
SMARTLIB_INTERNAL_SIGN=dev-internal-secret-please-change
INTERNAL_SIGN_SECRET=dev-internal-secret-please-change
LLM_PROVIDER=mock
```

### 4.1 灌入 20 万条测试书目

`deploy/sql/generated/` 已在 gitignore 中，不会把大数据集塞进仓库：

```bash
# 1) 生成（默认 20 万条；冒烟用 --count 200）
python scripts/gen_books.py --count 200000 --out deploy/sql/generated/

# 2) 灌数据（容器已起来的情况下）
#    注意顺序：先灌数据，再建 ngram 全文索引（反过来会因为写放大而极慢）
docker exec -i smartlib-mysql mysql -uroot -proot smartlib < deploy/sql/generated/books_bulk.sql

# 3) 再建全文索引（可重复执行，脚本用 information_schema 判重）
docker exec -i smartlib-mysql mysql -uroot -proot smartlib < deploy/sql/mysql-production-extras.sql

# 4) 让倒排索引重建（否则 /api/books 会用空索引，召回率假性为 0）
curl -X POST http://localhost:8081/api/admin/search/reindex
```

---

## 5. Kubernetes 部署

```bash
# 1) 先创建真实 Secret（secret.yaml 不在清单里，也不入库）
kubectl -n smartlib create namespace smartlib
kubectl -n smartlib create secret generic smartlib-secret \
  --from-literal=MYSQL_ROOT_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=MYSQL_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=REDIS_PASSWORD="" \
  --from-literal=SMARTLIB_INTERNAL_SIGN="$(openssl rand -hex 32)"

# 2) 试算（服务端校验 schema）
kubectl kustomize deploy/k8s | less
kubectl apply -k deploy/k8s --dry-run=server

# 3) 部署（想避免中间的 CrashLoopBackOff 可分两次：先数据层，再应用层）
kubectl apply -k deploy/k8s
kubectl -n smartlib get pods -w

# 4) 灰度发布与回滚（滚动更新 maxSurge=1 / maxUnavailable=0）
kubectl -n smartlib set image deployment/smartlib-backend backend=smartlib/backend:1.0.1
kubectl -n smartlib rollout status deployment/smartlib-backend
kubectl -n smartlib rollout undo deployment/smartlib-backend

# 5) 高可用验收（蓝图 §6.3 A2 验收线：删一个 Pod，压测流量 0 失败或 <0.1%）
kubectl -n smartlib delete pod -l app.kubernetes.io/name=smartlib-backend --field-selector=status.phase=Running | head -1
```

MySQL 初始化脚本建议用 ConfigMap 生成，别手工复制 SQL 文本：

```bash
kubectl -n smartlib create configmap smartlib-mysql-init \
  --from-file=01-schema.sql=deploy/sql/01-schema.sql \
  --from-file=02-seed.sql=deploy/sql/02-seed.sql \
  --from-file=03-mysql-production-extras.sql=deploy/sql/mysql-production-extras.sql \
  --dry-run=client -o yaml | kubectl apply -f -
```

> `mysql-statefulset.yaml` 里自带一个**占位** ConfigMap（只有一个说明用的 key），
> 目的是让清单单独 apply 也能通过；真实部署请用上面这条命令覆盖它。

---

## 6. Nginx 配置要点（`deploy/nginx/nginx.conf`）

| 能力 | 实现 | 对应蓝图 |
|---|---|---|
| 双实例负载 + 被动健康检查 | `upstream smartlib_backend { server backend-1:8080 max_fails=2 fail_timeout=10s; ... keepalive 32; }` | A2 |
| 入口限流（双层中的网关层） | `limit_req_zone`（读 200r/s、写 50r/s、Agent 20r/s）+ `limit_conn` | A4 |
| 内网接口隔离 | `location /internal/ { allow 172.16.0.0/12; allow 127.0.0.1; deny all; }` | §8.2 |
| SSE 流式透传 | `location /agent/` 内 `proxy_buffering off; proxy_cache off; proxy_read_timeout 600s; gzip off;` | §7 首 Token <1.5s |
| 探针入口 | `location = /actuator/health`（关限流、关 access_log） | A6 |
| 安全基线 | `server_tokens off` + `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` | — |
| 压缩 | `gzip on`（**刻意不含** `text/event-stream`，避免 SSE 被攒块） | — |

自检（不需要起整个 compose）：

```bash
docker run --rm -v "$PWD/deploy/nginx:/etc/nginx/conf.d:ro" nginx:1.27-alpine nginx -t
```

> 注意：本文件是 **http 上下文片段**（挂到 `/etc/nginx/conf.d/`），
> 因此不能出现 `events { }` / `http { }` 外壳，也不能把 `proxy_set_header` 写在 http 层。

---

## 7. CI（`.github/workflows/ci.yml`）

两个并行 job：

| job | 内容 |
|---|---|
| `backend` | JDK 21（`actions/setup-java` + Maven 缓存）→ `mvn -B -ntp test` → 上传 surefire 报告与 jar |
| `agent` | Python 3.12（`actions/setup-python` + pip 缓存）→ `pip install -r requirements.txt` → `python -m pytest -q`（`LLM_PROVIDER=mock`） |

- 只在 `backend/**`、`agent-service/**`、workflow 自身变化时触发。
- **刻意不做的事**：不跑压测（CI 机器不稳定，数字只会是噪声）、不做 Docker 构建推送
  （需要仓库凭证，且与 §6 的对比叙事无关）。压测请按本文件第 3 节在本机手工跑。

---

## 8. 已知限制（诚实清单）

1. **Dockerfile 位置与 build context 的冲突（本目录最重要的一个取舍）**
   Docker 要求 Dockerfile 位于 build context 之内；而本次交付要求 Dockerfile 放在 `deploy/`。
   两者同时成立的唯一组合是 **context = `deploy/`**，因此：
   - `docker-compose.yml` 里写的是 `context: .` + `dockerfile: Dockerfile.backend|.agent`；
   - **必须在 `deploy/` 目录下执行 compose**（`cd deploy && docker compose up -d --build`）；
   - `deploy/Dockerfile.*` 里的 `COPY backend/pom.xml` 依赖"`deploy/` 与 `backend/` 同级"这一布局。
   若想改成"仓库根目录执行 `docker compose -f deploy/docker-compose.yml up`"，
   需要把两个 Dockerfile 复制到仓库根目录，并把 compose 里的 build 段改成
   `context: ..` + `dockerfile: deploy/Dockerfile.backend`（但那样 Dockerfile 又落到 context 之外，
   Docker 会直接拒绝）—— 所以**推荐做法是保留现在的 `context: .`**。
   根目录变体的正确形式：在根目录放 Dockerfile 副本，compose 写
   `build: { context: ., dockerfile: Dockerfile.backend }`，两处 `COPY` 路径不变。

2. **Docker / K8s / Nginx 产物没有实际启动验证过**（本机没有 Docker，也没有 K8s 集群）。
   已验证与未验证的部分要分清楚：

   | 项目 | 状态 |
   |---|---|
   | 17 个 YAML（compose + k8s + CI）| ✅ 全部能 `yaml.safe_load_all` 解析；关键字段（副本数、滚动策略、探针、preStop、HPA 阈值、端口映射、healthcheck）逐条断言通过 |
   | `nginx.conf` | ✅ 花括号配对、无 `events`/`http` 外壳、关键指令（keepalive 32 / max_fails / 限流 / allow-deny / SSE 三件套 / server_tokens）存在性检查通过；❌ **没有用 `nginx -t` 真校验过**（需要 Docker） |
   | `Dockerfile.*` | ✅ 结构检查通过（多阶段、非 root、exec 形式 ENTRYPOINT/CMD、EXPOSE、先 COPY 依赖）；❌ **镜像从未构建过** |
   | `scripts/gen_books.py` | ✅ 真跑过：`--count 200`（样本已生成）、20 万条尺度的 ISBN 唯一性与校验位自检通过 |
   | `scripts/bench/*.py` | ✅ 真跑过（对着真实后端 8091）：六策略借阅压测 + 双引擎检索压测 + 双引擎召回率，退出码与 JSON 产物均正常 |
   | `scripts/bench/run_all.ps1` | ✅ 真跑过完整流程（PS 5.1），产出了 `summary.md`；❌ **从未用 Docker/Compose 起的后端跑过** |
   | `scripts/start-local.ps1` | ✅ 真跑过：在 PS 5.1 下识别出 `mvn.ps1` 无 BOM 并走 fallback，成功把后端拉起（H2、`Started SmartLibApplication`、`Tomcat started on port 8091`） |
   | `deploy/sql/01-schema.sql` + `02-seed.sql` | ✅ 真跑过：用 H2 2.4.240（`MODE=MySQL`）完整执行，行数与"库存/流水守恒、无负库存"不变式全部通过 |
   | `.github/workflows/ci.yml` | ❌ 未在真实 GitHub Runner 上跑过（路径、action 版本、命令都是标准写法） |

   **"能解析"不等于"能跑起来"**：首次 `docker compose up` / `kubectl apply -k` 时请预留排错时间。

3. **镜像构建未验证**：`backend` 镜像从未构建过（本机无 Docker），
   `agent` 镜像还依赖 `agent-service/requirements.txt` 与 `agent-service/app/` 存在。
   缺文件时构建会明确失败在对应 `COPY`，不会静默产出坏镜像。

4. **两个后端实例共享同一个镜像与同一份数据库**，因此"多实例"验证的是无状态与
   负载均衡，不是数据分片。HPA 也只在 K8s 环境生效（Compose 的 `deploy.resources`
   只作用于 `docker compose --compatibility` 或 Swarm）。

5. **S5（redis-lua）与 S6（mq-serial）的数字有前提**：
   - S5 依赖 Redis 可用；Redis 挂了后端会自动降级为 S2（`DegradeFallback`），
     此时 `summary.md` 里 S5 那行的数字其实是 S2 的。降级次数可在
     `/api/admin/reconcile` 的 `data.redisLuaDegradedCount` 看到，必须一起写进报告。
   - S6 在本地是"单线程 Dispatcher + 有界队列"的模拟实现，不是真实 RocketMQ 事务消息。

6. **压测客户端即瓶颈**：`load_borrow.py` / `search_bench.py` 用标准库 `http.client`，
   每请求新建一条 TCP 连接（无连接池复用）。所以 QPS 是**下界**而非服务端极限，
   要测上限请用 wrk/JMeter 并对比差异。

7. **每轮借阅压测都会真实扣库存**：多轮连续跑会把书借空，
   之后错误率上升是"库存耗尽"的正常业务失败，不是策略变差。
   对比多轮数据前请确认库存量，必要时重置数据（`docker compose down -v` 会重跑 initdb）。

8. **同一读者对同一本书只能借 1 册（蓝图 §2.2-5）**：并发抢同一本书必须使用读者池
   （`--reader-ids 1-30`），否则测到的是"重复借阅拦截率"而不是并发扣减能力。

9. **`/api/admin/**` 默认关闭**：`AdminController` 由 `smartlib.admin.enabled=true` 控制。
   本地用 `scripts/start-local.ps1`（已自动带上该参数），Compose 通过
   `SMARTLIB_ADMIN_ENABLED=true` 打开。生产环境应关闭或叠加鉴权（它能改库存策略、重建索引）。

10. **`deploy/sql/01-schema.sql` 与 `02-seed.sql` 是应用侧 SQL 的副本，不允许漂移**
    - 权威来源是 `backend/src/main/resources/db/schema.sql` 与 `db/data.sql`（应用启动时执行）；
    - 部署侧两份由 `python scripts/tools/check_sql_sync.py` 强制校验逐条一致（CI 也会跑）；
    - 两份 SQL 都只用 **H2 `MODE=MySQL` 与 MySQL 8.0 的交集语法**，因此两侧共用同一份内容，
      不存在"H2 专用 / MySQL 专用"的差异。详细规则见 `deploy/sql/README.md`。
    - 本次交付时实测过：把 `01-schema.sql` + `02-seed.sql` 用 H2 2.4.240
      （连接串 `jdbc:h2:mem:...;MODE=MySQL;DATABASE_TO_LOWER=TRUE`，与 local profile 一致）
      完整执行一遍，结果 24 书目 / 24 库存 / 12 读者 / 6 分类 / 4 借阅流水 / 1 预约 /
      3 库存流水 / 17 规则；负库存 0 行、对账违规 0 行。
    - 顺带修掉一个真实缺陷：早期 `01-schema.sql` 末尾有一句
      `ALTER DATABASE smartlib CHARACTER SET ...`，MySQL 接受但 **H2 直接语法报错**，
      会让 H2 环境初始化失败。已删除，字符集改由容器启动参数与建库语句保证。

11. **K8s 里的 Nginx**：`deploy/k8s/` 没有为 Nginx 单独建 Deployment —— K8s 环境用
    Ingress 承担入口职责（等价于 Compose 里 Nginx 的角色）。两套入口配置需要各自维护，
    改限流/超时策略时要**两边都改**（对照表见第 6 节）。

12. **PowerShell 脚本编码（本次实际踩坑，必须读）**
    PowerShell 5.1（Windows 自带）在**没有 UTF-8 BOM** 时按 ANSI 代码页读取 `.ps1`，
    含中文的文件会直接解析失败（报"字符串缺少终止符 / Unexpected token"这类完全对不上号的错误）。
    本目录交付的两个脚本按此规则处理：
    - `scripts/start-local.ps1`：**纯 ASCII**（注释与输出都是英文），因此不依赖 BOM，最稳；
    - `scripts/bench/run_all.ps1`：含中文输出，保存为 **UTF-8 with BOM**。
      ⚠️ 用某些编辑器（或"另存为 UTF-8"）保存会**丢掉 BOM**，之后 PS 5.1 就会解析失败。
      丢了就补回来（仓库根目录执行）：
      ```bash
      python -c "import io;p='scripts/bench/run_all.ps1';t=io.open(p,encoding='utf-8').read();io.open(p,'w',encoding='utf-8-sig',newline='').write(t)"
      ```
      或者改用 PowerShell 7 执行（`pwsh -File ...`）。

13. **`scripts/mvn.ps1`（不属于本次交付范围）在 PS 5.1 下无法直接执行**
    该文件含中文注释且**没有 UTF-8 BOM**，在 Windows PowerShell 5.1 下会被 ANSI 解码，
    报 `Unexpected token ... / The string is missing the terminator`，Maven 根本起不来。
    这不影响正确性，但会挡住"本地零依赖启动"这条路，所以 `start-local.ps1` 做了两件事：
    - 启动前**检测**该文件是否"含非 ASCII 且无 BOM"并给出修复建议；
    - 命中时自动改用**等价的 classworlds 直启**方式（与 `mvn.ps1` 内部实现一致：
      用 `java` 直接跑 `plexus-classworlds` launcher，绕开 `mvn.cmd` 的 cmd.exe 代码页问题），
      **不修改** `mvn.ps1` 本身。
    根治方式二选一：给 `mvn.ps1` 加 UTF-8 BOM（见第 12 条的 Python 一行命令，路径换成 mvn.ps1），
    或者用 PowerShell 7 跑所有脚本。

14. **`/actuator/health` 在无 Redis 的机器上返回 503**
    本地没有 Redis 时，`redis` 健康组件是 DOWN，整体 `status=DOWN`、HTTP 503，
    但 `db` 是 UP、应用完全可用（库存策略会自动降级到 DB 条件更新）。
    因此两个脚本的"等待就绪"判据都是：**HTTP 有响应 且 `components.db=UP`**，
    并把非 UP 组件打印出来。若只用 `status == UP` 判断，本地压测会永远等不到后端。
    K8s 里同理：`readinessProbe` 打 `/actuator/health/readiness`（probe 组只含
    liveness/readiness 状态，不聚合外部依赖），所以 Redis 抖动不会把后端 Pod 摘出负载。

15. **【后端缺陷·压测时实测确认】书目详情缓存不随库存变化失效 —— 需要后端同学修**
    - **现象**：借出 8 册后立即 `GET /api/books/{id}`，`availableCopies` 仍返回**旧值**；
      同一时刻内部签名接口 `GET /internal/agent/tools/books/{id}/stock` 返回正确值。
      实测 30 个借阅轮次**每一轮**都复现（见 `summary.md` 表 2 的「借后可借 vs 缓存读数」两列）。
    - **原因**：`BookService.getById` 走 `CacheGuard`（Caffeine L1 5min / Redis L2 30min），
      而后端**唯一**的 `StockService.StockChangeListener` 实现是 `InvertedIndexSearchEngine`
      （只更新倒排索引），**没有任何代码在库存变化时失效书目详情缓存**，只能等 TTL 到期自愈。
    - **影响**：读者/前端看到的"可借数"会短暂偏大（本项目 P1 超卖叙事的直接相关面）。
      **不影响扣减正确性**：`book_stock` 与 `stock_ledger` 守恒，逐阶段对账全部 PASS。
    - **本目录的处理（不修改 `backend/`）**：压测脚本一律从内部签名接口取权威库存，
      并把缓存读数单独列出来作对照；`load_borrow.py` 报告里会给出 `correctness.cacheStaleCycles`。
    - **修复建议**：库存变更时同步失效 `smartlib:book:{id}`，或让 `BookView` 的库存字段
      走更短 TTL/直读库存表。属于后端写路径，本次写入范围外。

16. **PowerShell 的两个"字面量陷阱"（本目录脚本已按此规避，改脚本时注意）**
    - PS 5.1 把 `“ ” ‘ ’`（中文弯引号）当作**引号定界符**，写在双引号字符串里会提前结束字符串。
      本目录脚本已全部改用 「」 或 ASCII 单引号。
    - 双引号字符串里想输出**一个反引号**必须写两个；写一个会被当转义符吞掉，
      若紧邻结尾引号（例如 ``` 代码块围栏）还会把结尾引号一起吞掉，导致解析失败。

---

## 9. 常用命令速查

```bash
# ---- 本地开发 ----
make run-local                 # 零依赖启动（等价于 start-local.ps1）
make build                     # 后端打包
make test                      # 后端测试 + Agent 测试
make ci                        # 本地复现 CI

# ---- 基准测试 ----
make bench                     # 一键全量，产物在 scripts/bench/results/summary.md
make bench-borrow              # 只跑借阅压测（含超卖检查）
make bench-search              # 只跑检索压测 + 召回率
make gen-books                 # 生成 20 万条书目到 deploy/sql/generated/

# ---- Compose ----
make compose-up                # = cd deploy && docker compose up -d --build
make compose-ps
make compose-logs
make compose-down
make compose-reset             # 删容器 + 数据卷（会清空数据）

# ---- K8s ----
cd deploy && kubectl kustomize k8s | less
kubectl apply -k deploy/k8s
```

> `Makefile` 里的 `make compose-*` 用 `docker compose -C deploy …` 切到 `deploy/`，
> 与"必须在 `deploy/` 下执行"这条要求一致。
