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
│   ├── 01-schema.sql                  表结构（与 backend/src/main/resources/db/schema.sql 同步）
│   ├── 02-seed.sql                    MySQL 版演示种子数据（14 本书 + 11 位读者 + 1 条逾期）
│   ├── mysql-production-extras.sql    生产增量优化：ngram 全文索引(Q4)/分区/账号权限/慢查询
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
    -BaseUrl http://localhost:8080 -BookId 5 -Threads 50 -Requests 300
```

产物（都在 `scripts/bench/results/`）：

| 文件 | 内容 |
|---|---|
| `summary.md` | 汇总 Markdown 表格（每个方案一行：并发/QPS/P50/P90/P99/错误率/成功数/超卖数/召回率）+ 对账结果 + 口径与限制 |
| `borrow_<strategy>.json` | 六种策略各自的原始压测数据 |
| `search_like.json` / `search_inverted-index.json` | 两个引擎的检索压测数据（含实测引擎名） |
| `recall_like.json` / `recall_inverted-index.json` | 两个引擎的召回率明细（逐条 query 命中情况） |
| `reconcile.json` | 对账接口原始响应 |
| `notes.txt` | 跳过的步骤与失败原因（如管理端点未开启） |

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

退出码约定（便于脚本判定）：

| 退出码 | 含义 |
|---|---|
| 0 | 正常完成 |
| 2 | 参数错误 |
| 3 | **发生超卖**（`load_borrow.py`：压测后 `availableCopies < 0`） |
| 4 | 全部请求失败（后端没起来 / 地址写错） |

### 3.3 召回率口径（必须和数字一起看）

- 数据集每条是 `{"query": "...", "expected_book_ids": [1, 2]}`，期望 id 取自
  `backend/src/main/resources/db/data.sql` 里**真实存在**的 24 本书。
- 单条召回率 = `|命中 ∩ 期望| / |期望|`；平均召回率 = 各条平均。
- 期望集是"至少应被召回"的人工小集合，不是完整相关文档集合 → 属**可用性召回**，
  不能当学术意义的 recall 用。这一点在 `summary.md` 和 JSON 里都写了。
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
STOCK_STRATEGY=redis-lua
SEARCH_ENGINE=inverted-index
SMARTLIB_ADMIN_ENABLED=true
SMARTLIB_INTERNAL_SIGN=dev-internal-secret-please-change
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

2. **全部 Docker / K8s / Nginx 产物都没有实际启动验证过。**
   本机没有 Docker（`docker` 不在 PATH），也没有可用的 K8s 集群。
   已验证的部分只有：YAML 全部能被 `yaml.safe_load_all` 解析、nginx.conf 花括号配对与
   关键指令存在性、PowerShell 脚本能被 PS 5.1 的 `Parser::ParseFile` 无错解析、
   Python 脚本实际跑过（含生成 200 条数据与对不可达端口压测的错误路径）。
   **"能解析"不代表"能跑起来"**：首次 `docker compose up` / `kubectl apply -k` 时请预留排错时间。

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

10. **`deploy/sql/01-schema.sql` 是 `backend/.../db/schema.sql` 的副本**：
    MySQL 容器的 initdb 无法挂软链，只能放真实文件。
    改表结构后必须同步（否则 MySQL 与 H2 两套环境的结构会漂移）：
    `cp backend/src/main/resources/db/schema.sql deploy/sql/01-schema.sql`

11. **K8s 里的 Nginx**：`deploy/k8s/` 没有为 Nginx 单独建 Deployment —— K8s 环境用
    Ingress 承担入口职责（等价于 Compose 里 Nginx 的角色）。两套入口配置需要各自维护，
    改限流/超时策略时要**两边都改**（对照表见第 6 节）。

12. **PowerShell 脚本编码**：`scripts/bench/run_all.ps1` 含中文输出，保存为
    **UTF-8 with BOM**。PowerShell 5.1 在没有 BOM 时会按 ANSI 代码页读文件，
    中文会变成乱码并可能直接导致解析失败（本次开发就踩了这个坑）。
    `scripts/start-local.ps1` 刻意只写 ASCII 注释与输出，因此不依赖 BOM。

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
