# SmartLib Agent 服务（Python / FastAPI）

高校图书馆智能借阅平台的 **Agent 服务**，为读者提供 7×24 的自然语言接待：
查书、查实时库存与馆藏位置、借书、续借、查在借与逾期、解答借阅规则、按偏好推荐。

- 契约来源：`docs/00-项目蓝图.md`（§3.2 技术栈、§4.3 目录结构、§6.4 G1~G5 方案、§7 SLO、§8.2 工具 API）
- 定位：Java 后端负责业务正确性（库存、规则、事务），Python 负责**理解与编排**
- **零 Key、零外部依赖即可完整运行与评测**（默认 `LLM_PROVIDER=mock`）

---

## 1. 五分钟跑起来（不需要任何 API Key）

```powershell
cd <仓库根>\agent-service

# 1) 起服务（默认 mock LLM + 内存会话，Java 后端没起也能启动）
& '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --port 8000

# 2) 问一句（同步接口，方便 curl/测试）
curl.exe -s -X POST http://127.0.0.1:8000/chat/sync `
  -H "Content-Type: application/json" `
  -d '{\"message\":\"找一下《算法导论》\",\"reader_id\":1001}'
```

想看 SSE 流式效果（含工具调用过程）：

```powershell
curl.exe -N -X POST http://127.0.0.1:8000/chat `
  -H "Content-Type: application/json" `
  -d '{\"message\":\"我有没有逾期的书\",\"reader_id\":1001}'
```

> Java 后端没启动时，工具会走熔断/降级路径并返回中文话术；
> 规则类问题会自动降级到**本地知识库 RAG**，依然答得出来并给出出处。

---

## 2. 切换到真实 LLM

服务用的是 OpenAI 兼容协议，DeepSeek / 通义 / 本地 vLLM 只改 base_url 与 model：

```powershell
$env:LLM_PROVIDER='openai_compat'
$env:LLM_API_KEY='sk-xxxx'
$env:LLM_BASE_URL='https://api.deepseek.com/v1'   # 或 https://dashscope.aliyuncs.com/compatible-mode/v1
$env:LLM_MODEL='deepseek-chat'                     # 或 qwen-plus / 本地模型名
& '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --port 8000
```

设计上做了两层兜底，**真实模型挂了不等于助手挂了**：

1. `LLMRouter` 捕获任何 LLM 异常并自动回落到 MockLLM，回复里会带
   `provider=mock(degraded_from_openai_compat)`，`/admin/metrics` 里能看到降级次数；
2. `AGENT_MODE=hybrid`（默认）下，高频意图（找书/查库存/查借阅/问规则）走**规则路由**，
   压根不调用 LLM。LLM 只用于长尾表达与自然语言组织。

---

## 3. 跑测试

```powershell
$env:PYTHONPATH='<仓库根>\.tools\pylibs'
& '<仓库根>\agent-service\.venv\Scripts\python.exe' -m pytest tests -q
```

**当前实测结果：`303 passed`（约 10 秒）。**

测试全部离线：Java 后端用 `httpx.MockTransport` 打进程内的 `FakeJavaBackend`，
LLM 用 MockLLM，**没有一个用例需要启动真实服务**。

| 测试文件 | 覆盖内容 |
|---|---|
| `test_signature.py` | 与后端 `InternalSignFilter` 逐字对齐的签名（含中文 query 固定向量） |
| `test_intent.py` | 意图路由（易混意图、置信度分档、LLM 兜底与失败回落） |
| `test_tools.py` | 8 个工具的 schema 校验、越权、二次确认、失败翻译、重试/熔断 |
| `test_rag.py` | 分词、BM25 数学性质（IDF/词频饱和/长度归一化）、向量召回、混合排序、切分 |
| `test_guardrail.py` | 12 条护栏规则逐条正反例 |
| `test_memory.py` | 会话滑动窗口、TTL、待确认写操作、读者画像 |
| `test_agent_e2e.py` | 端到端行为契约（证据、确认、越权、4 种模式、降级、记忆、SSE） |
| `test_api.py` | 7 个接口 + trace_id 贯穿 + OpenAPI 可生成 |

---

## 4. 跑评测

```powershell
$env:PYTHONPATH='<仓库根>\.tools\pylibs'
& '.\.venv\Scripts\python.exe' -m app.eval.run_eval --provider mock
```

输出 Markdown 表格到 stdout，结构化结果写 `app/eval/report.json`。退出码 0 表示全部通过（可用于 CI）。

常用参数：

| 参数 | 说明 |
|---|---|
| `--provider mock\|openai_compat` | LLM 提供方（默认继承环境变量 `LLM_PROVIDER`） |
| `--backend fake\|live` | 后端：`fake` 进程内模拟（默认，离线可跑）；`live` 打真实 Java 后端 |
| `--mode rule\|llm\|hybrid\|plan` | 覆盖 Agent 模式，用于对比 G1/G2/G4/G5 |
| `--category <类别>` | 只跑某一类用例（如 `privilege`） |
| `--limit N` | 只跑前 N 条（调试） |

**关于 `--backend` 的诚实说明**：`fake` 用进程内模拟数据（`app/eval/fake_backend.py`），
目的是让指标衡量 **Agent 的决策质量**，而不是"后端有没有开"。
`report.json` 里会记录 `"backend": "fake" | "live"`，两者不会混为一谈。
真实联调验收请用 `--backend live`（需先启动 backend 并保证 `SMARTLIB_INTERNAL_SIGN` 一致）。

最近一次 `--provider mock --backend fake` 的实测结果（详见 `app/eval/report.json`）：

| 指标 | 实测值 | 蓝图 §7 目标 | 是否达标 |
|---|---|---|---|
| 意图识别准确率 | 100.00% | ≥ 92% | ✅ |
| 工具选择准确率 | 100.00% | — | — |
| 工具参数正确率 | 100.00% | ≥ 95% | ✅ |
| 端到端任务成功率 | 100.00% | ≥ 88% | ✅ |
| 事实性回答有工具证据比例 | 100.00% | 100% | ✅ |
| 平均步数 / 平均延迟 | 0.92 步 / 0.8 ms | — | — |

> 数据集 49 条中文用例（8 类：意图、工具选择、参数、多步、RAG 事实、幻觉诱导、越权、闲聊）。
> 延迟数字是 mock LLM + 进程内后端的量级，**不代表真实链路性能**；
> 真实链路请用 `--provider openai_compat --backend live` 重跑并替换本表。

---

## 5. 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat` | **SSE 流式**对话（`text/event-stream`） |
| POST | `/chat/sync` | 同步对话，返回完整结构化结果（评测/测试主入口） |
| GET | `/health` | 健康检查（含 Java 后端与熔断器状态、知识库 chunk 数） |
| GET | `/tools` | 列出 8 个工具及其参数 JSON Schema |
| POST | `/recommend` | 独立推荐接口（界面用，不过 LLM） |
| GET | `/admin/metrics` | 滚动窗口指标（延迟 P50/P95/P99、步数、证据比例、会话数…） |
| GET | `/admin/traces/{trace_id}` | 按 trace_id 捞一次对话的完整过程 |
| GET | `/admin/traces` | 最近若干条 trace 概要 |

**所有响应都带 `trace_id`**（含错误响应），响应头 `X-Trace-Id` 与响应体里的值一致。

SSE 事件协议：

```
event: start        {"type":"start","trace_id":...,"session_id":...,"mode":"hybrid"}
event: intent       {"type":"intent","intent":"my_loans","confidence":0.95,...}
event: tool_call    {"type":"tool_call","tool":"my_loans","step":1,"args":{...}}
event: tool_result  {"type":"tool_result","tool":"my_loans","ok":true,"summary":...}
event: token        {"type":"token","text":"您当前有 ..."}   ← 增量文本
event: done         {"type":"done","result":{...完整结果...}}
event: error        {"type":"error","message":"..."}
```

前端把 `token` 拼起来就等于 `done.result.reply`（有单测保证，避免流式渲染缺字）。

---

## 6. 与 Java 后端的内部签名协议（联调必读）

调用 `/internal/agent/**` 时带两个头：

| 头 | 值 |
|---|---|
| `X-Internal-Timestamp` | 当前 Unix 时间戳（**秒**，整数） |
| `X-Internal-Sign` | 小写十六进制 HMAC-SHA256 |

```
canonical = timestamp + "\n" + METHOD(大写) + "\n" + pathWithQuery + "\n" + bodyUtf8
sign      = hex_lowercase( HMAC_SHA256( secret, canonical ) )
```

精确定义（与后端 `InternalSignFilter` 已冻结对齐）：

- `timestamp`：与 `X-Internal-Timestamp` **完全相同的字符串**，不重新格式化、不补零；
- `METHOD`：**大写**，如 `GET` / `POST`；
- `pathWithQuery`：`getRequestURI() + "?" + getQueryString()`，
  **是 HTTP 请求行里百分号编码后的形式**，例如
  `/internal/agent/tools/policy?topic=%E7%BB%AD%E5%80%9F`；
  不含协议、域名、端口；
- `bodyUtf8`：请求体 UTF-8 字节直接解出的字符串；**无 body 时为空串**，
  但仍保留前面的 `\n`（规范串以 `\n` 结尾）；
- `secret`：`SMARTLIB_INTERNAL_SIGN`，默认 `dev-internal-secret-please-change`
  （与 backend `application.yml` 一致）；后端容忍 ±300 秒时间偏差（防重放）。

### 实现方式（重要，别改成手工拼串）

`JavaClient.request` 走的是**两阶段**：

```python
request = client.build_request(method, path, params=..., content=body, headers=...)
path_with_query = encoded_path_with_query(request)   # = request.url.raw_path.decode("ascii")
for k, v in self._sign_headers(method, path_with_query, body, trace_id).items():
    request.headers[k] = v
resp = await client.send(request)
```

**为什么要这样**：签名必须覆盖"实际发出去的那个请求目标"，
而百分号编码规则（safe 字符集、中文 UTF-8 编码、空格与 `+`）属于 httpx 的实现细节。
只有从构造好的请求对象上读 `raw_path`，才能保证"签的就是发的"。

> 踩坑记录：早期实现手工把"未编码"的中文 query 拼进规范串（`?topic=续借`），
> 而 Java 侧 `getQueryString()` 拿到的是 `%E7%BB%AD%E5%80%9F`，
> 于是**凡 query 里带中文的请求一律 401**。ASCII 参数下不会暴露，
> 属于典型的"只在非 ASCII 数据上才炸"的缺陷。
> 现在 `tests/test_signature.py` 里有针对性的回归用例 + 固定向量（含期望的 64 位小写 hex）。

---

## 7. 目录结构（严格按蓝图 §4.3）

```
agent-service/
├── app/
│   ├── main.py                 FastAPI 入口、路由装配、生命周期、trace 中间件
│   ├── config.py               配置（Pydantic Settings）+ knowledge_path 反推
│   ├── errors.py               异常与"给用户看的友好中文话术"映射表
│   ├── metrics.py              进程内滚动窗口指标（对齐蓝图 §7 SLO）
│   ├── tool_schemas.py         8 个工具的 Pydantic 参数模型（强校验，集中定义）
│   ├── api/
│   │   ├── chat.py             POST /chat（SSE）+ POST /chat/sync
│   │   ├── tools.py            GET /health、GET /tools
│   │   ├── recommend.py        POST /recommend
│   │   ├── admin.py            GET /admin/metrics、/admin/traces*
│   │   └── schemas.py          API 层请求/响应模型
│   ├── agent/
│   │   ├── runtime.py          Agent 主循环（ReAct + Plan-Execute 双模式）
│   │   ├── intent.py           意图识别（规则优先 + LLM 兜底）
│   │   ├── planner.py          多步任务拆解（含业务依赖处理）
│   │   ├── executor.py         工具执行（白名单/校验/单步超时/循环检测/埋点）
│   │   ├── prompts.py          所有 Prompt 模板集中管理（带版本号）
│   │   └── guardrail.py        12 条确定性护栏规则
│   ├── llm/
│   │   ├── base.py             统一契约（ChatMessage / LLMResult / ToolCall）
│   │   ├── mock.py             确定性规则模拟 LLM（离线可跑的关键）
│   │   ├── openai_compat.py    OpenAI 兼容协议（DeepSeek/通义/vLLM）
│   │   └── router.py           按配置选 provider + 失败自动回落 Mock
│   ├── tools/
│   │   ├── registry.py         工具注册表（白名单唯一来源）
│   │   ├── base.py             Tool 基类 / ToolContext / ToolResult / ToolError
│   │   ├── search_books.py     search_books   → POST /internal/agent/tools/search-books
│   │   ├── get_stock.py        get_stock      → GET  .../books/{id}/stock
│   │   ├── borrow.py           borrow         → POST .../borrow          （写，需确认）
│   │   ├── renew.py            renew          → POST .../renew           （写，需确认）
│   │   ├── my_loans.py         my_loans       → GET  .../readers/{id}/loans
│   │   ├── get_reader_profile.py  get_reader_profile → .../readers/{id}/profile
│   │   ├── policy_qa.py        policy_qa      → GET  .../policy?topic=...
│   │   └── recommend.py        recommend      → .../books/{id}/similar、.../books/hot
│   ├── rag/
│   │   ├── loader.py           知识库加载 + 按 Markdown 标题切 section（带 breadcrumb）
│   │   ├── splitter.py         中文分词（bigram/trigram）+ 文本规范化
│   │   ├── bm25.py             自研 BM25（k1=1.2、b=0.75，标准库实现）
│   │   ├── vector_store.py     numpy 余弦 + 确定性哈希嵌入降级
│   │   └── retriever.py        混合召回（加权融合 + 去重 + 相关性度量）
│   ├── memory/
│   │   ├── session_store.py    会话短期记忆（滑动窗口 + TTL + 待确认写操作）
│   │   └── reader_profile.py   读者画像长期记忆（偏好分类、最近借阅）
│   ├── client/
│   │   └── java_client.py      调 Java 后端（签名/超时/重试/熔断/trace 透传）
│   ├── knowledge/              9 篇知识库文档（借阅规则、册数与期限、逾期与罚金、
│   │                           预约、续借、馆藏分布与楼层、开放时间、学科服务、FAQ）
│   └── eval/
│       ├── dataset.jsonl       49 条中文评测用例（8 类）
│       ├── run_eval.py         评测入口（可独立运行，写 report.json）
│       ├── metrics.py          指标计算（含"不可评测"的显式处理）
│       ├── fake_backend.py     评测用进程内模拟后端
│       └── report.json         最近一次评测结果（由 run_eval 生成）
├── tests/                      pytest 用例（303 passed，全部离线）
├── requirements.txt
├── pytest.ini
├── .env.example
└── README.md
```

---

## 8. 环境变量表

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 服务监听地址 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `AGENT_MODE` | `hybrid` | Agent 方案：`rule`(G1) / `llm`(G2) / `hybrid`(G5) / `plan`(G4) |
| `MAX_STEPS` | `5` | ReAct 最大步数（蓝图硬约束，配置范围 1~5） |
| `STEP_TIMEOUT_SECONDS` | `3.0` | 单步（单次工具调用）超时 |
| `LOW_CONFIDENCE_THRESHOLD` | `0.45` | 低于此置信度转人工 |
| `MAX_OUTPUT_CHARS` | `800` | 输出长度上限（护栏优雅截断） |
| `LOOP_REPEAT_LIMIT` | `2` | 同一工具+同一参数重复调用上限（循环检测） |
| `JAVA_BASE_URL` | `http://127.0.0.1:8080` | Java 后端地址 |
| `JAVA_TIMEOUT_SECONDS` | `3.0` | 后端调用超时（预算覆盖全部重试） |
| `JAVA_RETRIES` | `2` | 重试次数（只重试幂等安全请求） |
| `JAVA_CIRCUIT_FAIL_THRESHOLD` | `5` | 连续失败多少次打开熔断 |
| `JAVA_CIRCUIT_OPEN_SECONDS` | `10.0` | 熔断打开后冷却时长 |
| `JAVA_CIRCUIT_HALF_OPEN_PROBES` | `3` | 半开状态放行的探测请求数 |
| `INTERNAL_SIGN_SECRET` | `dev-internal-secret-please-change` | 内部签名密钥（**必须与 backend 一致**） |
| `INTERNAL_SIGN_ENABLED` | `true` | 是否加签名头（本地免签名联调可关） |
| `LLM_PROVIDER` | `mock` | `mock`（离线确定性）/ `openai_compat`（真实模型） |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容端点 |
| `LLM_API_KEY` | 空 | API Key（`openai_compat` 必填） |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `LLM_TIMEOUT_SECONDS` | `30.0` | LLM 调用超时 |
| `LLM_MAX_TOKENS` | `1024` | 单次生成上限 |
| `LLM_TEMPERATURE` | `0.2` | 采样温度（图书馆场景要稳，取低值） |
| `KNOWLEDGE_DIR` | `app/knowledge` | 知识库目录（相对包路径） |
| `RAG_TOP_K` | `4` | 召回条数 |
| `RAG_BM25_WEIGHT` | `0.7` | BM25 融合权重 |
| `RAG_VECTOR_WEIGHT` | `0.3` | 向量融合权重 |
| `RAG_ENABLE_VECTOR` | `true` | 是否启用向量召回（无 Key 时用哈希嵌入） |
| `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` | `320` / `60` | 超长 section 的窗口切分参数 |
| `SESSION_MAX_TURNS` | `10` | 会话滑动窗口保留的消息条数 |
| `SESSION_TTL_SECONDS` | `1800` | 会话过期时间 |
| `PROFILE_TTL_SECONDS` | `86400` | 读者画像过期时间 |

---

## 9. 关键设计取舍

### 9.1 为什么 MockLLM 不是"假数据生成器"

它的输出格式与真实 LLM 完全一致（`tool_calls` + 自然语言），
所以从 `mock` 切到 `openai_compat` 时**上层 runtime 一行都不用改**。
它抓的是"对话策略"（该不该调工具、调哪个、参数怎么组装），
而不是"编一份看起来像的回答"。同一输入永远同一输出，评测才可复现。

### 9.2 规则优先 + LLM 兜底（G5）

图书馆场景意图分布极度倾斜，四类高频意图（找书/查库存/查借阅/问规则）占 80% 以上，
且判别特征明确（书名号 → 找书；"几本/在几楼/罚金" → 规则）。
用规则判 <1ms、0 token、100% 可复现；把这种问题交给 LLM 只会得到
"98% 准 + 每次不一样 + 要花钱"。LLM 的价值在长尾表达。

### 9.3 中文检索为什么用 bigram + trigram 两套

- **bigram** 用于建倒排索引与 BM25 打分：不需要词典，对新词/术语（"索书号""到馆保留"）友好；
- **trigram** 用于**相关性判定**：bigram 太碎（"预约到馆保留几天"会切出"约保""留几"
  这种不存在的组合），命中与否说明不了问题；trigram 的信息量接近"词"。

相关性闸门的口径（`policy_qa.MIN_RELEVANCE_RATIO`）是**在本项目知识库上实测标定**的：
28 条应命中的规则问句 + 14 条域外问句，取 ratio≥0.20。

### 9.4 护栏为什么是代码而不是提示词

提示词是概率性的，护栏必须确定性。12 条规则全部有单测，且**正反例都有**
（例如敏感词表要能拦住"代写论文"，但不能误杀"解剖学图谱"）。

### 9.5 `availableCopies = -1` 的语义

后端约定的**降级态**："库存暂时无法确认"，与 `0`（确实没有可借复本）完全不同。
`get_stock` 与回答渲染都做了分支，话术分别是"库存暂时查不到，请稍后再试"与
"目前没有可借复本，可以预约"。**说错这一句会直接把能借的书劝退。**

### 9.6 写操作的四道闸门

身份（readerId 必须等于会话 readerId）→ 确认（`confirm=true` 才下单，
且**明确确认词**才算，疑问句不算）→ 幂等（稳定 idempotencyKey）→ 降级
（超时≠失败，话术是"请到我的借阅核对"而不是"借阅失败"）。

> 实测踩过的坑：`readerId` 曾经被消息文本里的编号覆盖
> （"帮我借读者 2002 的书"能把自己伪装成 2002），
> 现在会话身份是权威值，文本里的编号只作为 `claimed_reader_id` 交给护栏比对。
> 对应回归用例：`tests/test_agent_e2e.py::test_claimed_reader_never_overrides_session_identity`。

---

## 10. 反幻觉硬约束：book_id 只能来自工具（G-13）

这是**真实联调暴露并修复**的一条核心不变式，单独写一节。

### 现象

联调时 `《计算机网络》还有几本？` 与 `帮我借《算法导论》` 都失败，Java 侧日志：

```
WARN GlobalExceptionHandler - 业务异常 code=2002 msg=图书不存在：id=102
WARN GlobalExceptionHandler - 业务异常 code=2002 msg=图书不存在：id=101
```

### 根因

`MockLLM` 曾用一张"书名 → id"的本地表**直接产出 `book_id`**，而表里的
`101/102` 只是离线假数据的 id —— 真实后端是 `算法导论=5`、`计算机网络=3`。
模型（无论 Mock 还是真实 LLM）都可能编出后端不存在的 id，这类缺陷**必然发生**。

### 修复（三层，缺一不可）

1. **运行时不变式**：`book_id` 的唯一合法来源是**工具结果**。
   `AgentRuntime._resolve_book_id` 只从 `search_books` 的候选里取，支持三种指代：
   显式书名（容忍"（第3版）"这类模糊）、序数（"第 2 本"）、唯一结果直接采用。
2. **模型给的 id 一律丢弃**：拿到模型参数时剥掉 `book_id`/`bookId`，
   并记一次 `unsourced_book_id_blocked` 计量（`/admin/metrics` 可见
   "模型到底编了多少次"）。
3. **前置核验**：执行 `get_stock`/`borrow` 前若没有可信 id，自动补一次
   `search_books`（标记 `internal=true`）；借书前再补一次 `get_stock` 确认
   **实时**可借复本。拿不到候选时如实回答"没找到这本书"，
   **绝不带着猜测的 id 去调后端**。

MockLLM 也不再产出任何 `book_id` —— 它只表达"书名"意图，与真实 LLM 走
**同一条**解析路径，离线测出来的行为对线上才有参考价值。

### 配套写操作护栏（G-13_write_needs_prerequisite）

拿不到"这本书存在且有馆藏"的证据时，**不允许**把写操作当成"待确认"抛给读者
—— 否则读者确认之后必然失败。原先的表现正是"404 之后仍在追问
'要我现在就为您办理借阅吗'"。现在这种情况会如实告知并停止。

### 工具结果字段归一化（联调实测的真实字段名）

| 接口 | 蓝图 §8.2 写的 | 真实后端返回 | Agent 处理 |
|---|---|---|---|
| `search-books` | 未明确主键名 | `items[].id` | `bookId`/`book_id`/`id` 都归一化 |
| `books/{id}/stock` | `available,total` | `availableCopies,totalCopies` | 两种都兼容 |
| `borrow` | `dueDate` | `dueAt` | 两种都兼容 |
| `renew` | `newDueDate` | `newDueAt` | 两种都兼容 |
| `readers/{id}/loans` | `dueDate` | `dueAt` | 两种都兼容 |

### 响应结构

`/chat/sync` 与 SSE `done` 都带 `schema_version`（当前 `1.1.0`）。
`tool_calls` **只含读者要求的那次调用**；运行时内部补的步骤在
`/admin/traces/{trace_id}` 的 `internal_tool_calls` 里可查。

真机自检脚本：`python scripts/live_check.py --reader-id 3`（需先起 Java 后端）。

---

## 11. 已知限制与未验证项

诚实清单（写清楚比藏起来有用）：

1. **已完成真机联调，但仍有未覆盖的部分**：已用真实 Java 后端（8080）+ Agent（8000）
   跑通以下链路：
   - 中文 query 的 HMAC 签名端到端通过（`search-books`、`books/{id}/stock` 均 200）；
   - `《算法导论》还有几本` → 内部检索解析出真实 `book_id=5` → 库存返回
     "馆藏 4 册，当前可借 3 册，三楼东区 TP301/233"；
   - `帮我借《算法导论》` → 库存核验通过 → 待确认 → `确认借阅` 真正落库
     （借阅记录号 1023，应还日期由后端给出）。
   **仍未逐一验证**（有联调环境时应补）：
   - `renew` 的真实续借（`newDueAt` 已做字段兼容但未实跑）；
   - `policy_qa` 的 `/internal/agent/tools/policy` 真实规则库应答；
   - `/internal/agent/tools/books/{id}/similar` 与 `.../books/hot`（recommend 依赖，
     蓝图 §8.2 未单列）；
   - 真实库的读者 id 是 1/2/3（不是 1001/1002），`readerId` 由会话传入，
     需上层（小程序/网关）保证身份正确。
2. **真实 LLM 未验证**：`openai_compat` 按 OpenAI 协议实现（含宽松 JSON 解析、
   流式 SSE、tool_calls），但没有真实 Key 跑过端到端。切换后建议先跑
   `--provider openai_compat --backend live` 观察指标。
3. **评测延迟不代表真实性能**：mock LLM + 进程内后端是 ~1ms 量级；
   真机实测单次检索约 3~150ms（取决于检索引擎）。首 Token 延迟
   （蓝图 §7 目标 <1.5s）本脚本不评测，由 SSE 流式路径覆盖
   （`/chat` 的 `done` 事件给出 `first_token_ms`）。
4. **记忆是进程内的**：多实例部署会话不共享，需要换 Redis 实现
   （`session_store` / `reader_profile` 的接口保持不变即可替换）。
5. **相关性问题**：纯词法检索存在词面巧合导致的误召回。
   实测漏网的例子是"食堂几点开饭"（与"图书馆几点开门"共享"几点"），
   会被答成开放时间。彻底解决需要中文分词/embedding（配 Key 时已支持
   真实 embedding，会把哈希嵌入降级掉）。
6. **知识库里有 1 条覆盖缺口**："图书馆有没有自习室"在知识库中没有对应条目，
   助手会如实回答"没查到"并引导到服务台（这是期望行为，不是 bug；
   计入评测时单独标注了口径）。
7. **`app/eval/report.json` 是生成物**：每次跑评测会覆盖，不要手工编辑。

---

## 12. 常见问题

**Q：起服务时提示 Java 后端连不上？**
A：正常。服务本身不依赖后端存活，工具调用会走熔断/降级并返回中文话术。
配好 `JAVA_BASE_URL` 与 `INTERNAL_SIGN_SECRET` 后即可联调。

**Q：所有内部接口都 401？**
A：99% 是签名问题。依次确认：① `INTERNAL_SIGN_SECRET` 与 backend 的
`SMARTLIB_INTERNAL_SIGN` 一致；② 机器时间与服务端偏差在 ±300 秒内；
③ 请求确实带了 `X-Internal-Timestamp` 与 `X-Internal-Sign`。

**Q：为什么 `AGENT_MODE=llm` 时结果反而更差？**
A：MockLLM 在 `llm` 模式下没有规则路由的优先级保证，纯靠它自己的判别规则，
这正是 G1/G2/G5 对比实验想展示的效果：**图书馆场景规则路由更稳**。
真实 LLM 下差距会缩小，但成本与波动性会上升。
