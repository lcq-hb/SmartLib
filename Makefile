# =============================================================================
# SmartLib Makefile
#
# 定位：把"常用的几条命令"固化成短目标，避免每次翻文档。
#       所有目标都只是对 scripts/ 与 deploy/ 里真实命令的封装，不引入额外逻辑。
#
# 【运行环境】本文件按 Linux/macOS（以及 Windows 上的 WSL / Git-Bash）编写：
#   SHELL=/bin/sh，并用到 find / rm / mkdir -p。
#   Windows 原生 cmd/PowerShell 下请直接跑等价脚本，更可靠：
#       powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1
#       powershell -ExecutionPolicy Bypass -File scripts/bench/run_all.ps1
#       docker compose -f deploy/docker-compose.yml up -d --build
#   需要 GNU Make 4.0+（help 目标里用不到，但 gen-books 用了 $(or ...) 函数）。
#
# 【依赖提醒】run-local 需要 .tools/apache-maven-3.9.9（仓库自带 Maven 启动器要用）。
#   bench 需要后端已经在跑（本地零依赖启动：make run-local）。
#
# 常用变量（可在命令行覆盖）：
#   make bench BASE_URL=http://localhost:8081 BOOK_ID=5 THREADS=200 REQUESTS=400
# =============================================================================

SHELL      := /bin/sh
PYTHON     ?= python
# 必须用 -C deploy 切到 deploy/ 目录：Dockerfile 与 build context 都在那里
# （Docker 要求 Dockerfile 位于 context 之内，详见 deploy/README.md 已知限制第 1 条）
COMPOSE    ?= docker compose -C deploy
BASE_URL   ?= http://localhost:8080
BOOK_ID    ?= 5
THREADS    ?= 50
REQUESTS   ?= 300
IMAGE_TAG  ?= 1.0.0

# PowerShell 调用前缀（Windows 上必需；Linux/macOS 上装了 pwsh 也能用）
PS         := powershell -NoProfile -ExecutionPolicy Bypass -File

.DEFAULT_GOAL := help

.PHONY: help build test run-local bench bench-borrow bench-search gen-books \
        compose-up compose-down compose-logs logs compose-ps compose-build \
        compose-reset clean ci

# ----------------------------------------------------------------------------
# help：列出所有目标（默认目标）
# ----------------------------------------------------------------------------
help:
	@echo "SmartLib 常用命令"
	@echo ""
	@echo "  开发"
	@echo "    make build         编译后端（跳过测试）"
	@echo "    make test          跑后端测试 + Agent 测试"
	@echo "    make run-local     零依赖本地启动（后端 H2 + Agent MockLLM）"
	@echo "    make ci            本地复现 CI：mvn test + pytest"
	@echo ""
	@echo "  基准测试（需要后端已启动）"
	@echo "    make bench         一键跑全部方案并生成 summary.md"
	@echo "    make bench-borrow  只跑当前策略的借阅压测 + 超卖检查"
	@echo "    make bench-search  只跑检索压测 + 召回率"
	@echo "    make gen-books     生成 20 万条测试书目（deploy/sql/generated/）"
	@echo ""
	@echo "  部署"
	@echo "    make compose-up    起 MySQL/Redis/后端x2/Agentx2/Nginx"
	@echo "    make compose-down  停并删除容器（保留数据卷）"
	@echo "    make compose-logs  跟踪全部日志"
	@echo "    make logs          同 compose-logs"
	@echo "    make compose-ps    查看容器与健康状态"
	@echo ""
	@echo "  其他"
	@echo "    make clean         清理构建产物与生成数据（不动 .tools/）"
	@echo ""
	@echo "  可覆盖变量：BASE_URL / BOOK_ID / THREADS / REQUESTS / IMAGE_TAG"
	@echo "  例：make bench BASE_URL=http://localhost:8081 BOOK_ID=5 THREADS=200 REQUESTS=400"

# ----------------------------------------------------------------------------
# build：后端打包（跳过测试，因为压测/部署只关心产物）
# ----------------------------------------------------------------------------
build:
	@echo ">>> mvn package -DskipTests"
	$(PS) scripts/mvn.ps1 -f backend/pom.xml -DskipTests package

# ----------------------------------------------------------------------------
# test：后端测试 + Agent 测试
# ----------------------------------------------------------------------------
test:
	@echo ">>> backend: mvn test"
	$(PS) scripts/mvn.ps1 -f backend/pom.xml test
	@echo ">>> agent-service: pytest"
	$(PYTHON) -m pytest -q agent-service

# ----------------------------------------------------------------------------
# ci：本地复现 GitHub Actions 的两个 job（顺序跑，便于单机排查）
# ----------------------------------------------------------------------------
ci: test

# ----------------------------------------------------------------------------
# run-local：零依赖启动（H2 + MockLLM）
# ----------------------------------------------------------------------------
run-local:
	@echo ">>> start-local.ps1（后端 local/H2 + Agent mock LLM）"
	$(PS) scripts/start-local.ps1

# ----------------------------------------------------------------------------
# bench：一键基准（六策略 + 检索 + 召回率 + 对账 -> summary.md）
# ----------------------------------------------------------------------------
bench:
	@echo ">>> run_all.ps1 base=$(BASE_URL) book=$(BOOK_ID) threads=$(THREADS)"
	$(PS) scripts/bench/run_all.ps1 -BaseUrl $(BASE_URL) -BookId $(BOOK_ID) -Threads $(THREADS) -Requests $(REQUESTS)

# 只跑一次借阅压测（默认 optimistic-db 策略；切换策略用 run_all.ps1）
bench-borrow:
	@mkdir -p scripts/bench/results
	$(PYTHON) scripts/bench/load_borrow.py --url $(BASE_URL) --book-id $(BOOK_ID) \
		--threads $(THREADS) --requests $(REQUESTS) --reader-ids 1-30 \
		--label manual --out scripts/bench/results/borrow_manual.json

# 只跑检索压测 + 召回率（引擎由后端配置决定）
bench-search:
	@mkdir -p scripts/bench/results
	$(PYTHON) scripts/bench/search_bench.py --url $(BASE_URL) --engine current-config \
		--threads $(THREADS) --requests $(REQUESTS) \
		--queries-file scripts/bench/recall_dataset.json \
		--recall-check scripts/bench/recall_dataset.json \
		--label manual --out scripts/bench/results/search_manual.json

# ----------------------------------------------------------------------------
# gen-books：生成测试书目（产物目录已 gitignore）
# ----------------------------------------------------------------------------
gen-books:
	@echo ">>> gen_books.py --count 200000（可用 COUNT=2000 覆盖）"
	$(PYTHON) scripts/gen_books.py --count $(or $(COUNT),200000) --out deploy/sql/generated/

# ----------------------------------------------------------------------------
# Docker Compose
# ----------------------------------------------------------------------------
compose-up:
	$(COMPOSE) up -d --build
	@echo ">>> 等待健康后访问 http://localhost/ 与 http://localhost/agent-health"

compose-down:
	$(COMPOSE) down

# 删除容器 + 数据卷（会清空 MySQL/Redis 数据，下次启动重跑 initdb 脚本，谨慎）
compose-reset:
	$(COMPOSE) down -v

compose-ps:
	$(COMPOSE) ps

compose-build:
	$(COMPOSE) build

compose-logs:
	$(COMPOSE) logs -f --tail=200

# 别名：docs/07 与 README 里都用 logs
logs: compose-logs

# ----------------------------------------------------------------------------
# clean：只清理"可再生产物"，绝不删 .tools/（那里是本地 Maven 与依赖仓库）
# ----------------------------------------------------------------------------
clean:
	@echo ">>> 清理后端构建产物"
	-rm -rf backend/target
	@echo ">>> 清理 Python 缓存"
	-find . -type d -name __pycache__ -prune -exec rm -rf {} +
	-rm -rf agent-service/.pytest_cache
	@echo ">>> 清理生成的测试数据与压测结果"
	-rm -f deploy/sql/generated/books_bulk.sql deploy/sql/generated/books_bulk.csv
	-rm -f deploy/sql/generated/book_stock_bulk.csv
	-rm -f scripts/bench/results/*.json
	@echo ">>> 保留：.tools/（本地 Maven 与 m2repo）、scripts/bench/results/summary.md"
