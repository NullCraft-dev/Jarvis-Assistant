# 开发文档

## 文档目的

本文档用于规范各模块在开发时需要交付什么、说明什么、如何描述理论依据、实现方式和数据流。

它不负责项目愿景、整体架构或技术选型。这些内容分别由其他文档负责。

## 模块开发说明模板

每个模块在实现时应包含以下内容：

```text
1. 模块职责
2. 不负责什么
3. 核心理论或设计依据
4. 对外接口
5. 内部实现
6. 输入数据
7. 输出数据
8. 数据流动
9. 错误处理
10. 权限与安全影响
11. 测试方式
12. 日志与可观测性
```

## 工程分层

项目开发按以下工程层级组织。Claude Code 开发时应优先保持层级边界清晰，Codex 审查时也按这些边界检查是否出现越层调用。

```text
1. Frontend UI Layer
2. Frontend State Layer
3. Client API / Transport Layer
4. Go Gateway / Runtime Orchestrator Layer
5. Redis Runtime Bus Layer
6. Python Agent Worker Runtime Layer
7. Capability Adapter Layer
8. Storage Access Layer
9. Persistence Layer
```

整体调用方向：

```text
Frontend UI
-> Frontend State
-> Client API / Transport
-> Go Gateway / Runtime Orchestrator
-> Python Control Plane (short requests) or Redis Runtime Bus (runtime events)
-> PostgreSQL (persistence) / Python Agent Worker Runtime
-> Capability Adapter
-> Storage Access (Repository interfaces)
-> Persistence (PostgreSQL)
```

> **架构迁移中（2026-07-14）**：目标架构新增 Python Control Plane 层，作为 Go Gateway 的唯一持久化入口。Go 不再直接访问数据库。新增 PostgreSQL 作为唯一持久化真相。
-> Persistence
```

运行时可以通过 EventBus 把 Runtime 事件反向推送给 Frontend State，但业务调用不应该绕过上面的层级边界。

### 1. Frontend UI Layer

负责 Vue 页面和组件。

包括：

```text
Command Center
Task Dashboard
Run Timeline
Right Inspector
Permission Dialog
Settings
Tools / MCP 页面
Memory 页面
Dev Panel
```

负责：

- 展示数据。
- 收集用户输入。
- 触发用户操作。
- 展示运行状态、权限请求、工具调用和错误。

不负责：

- 直接调用 raw transport、IPC 或后端 handler。
- 直接访问数据库、本地文件、Shell 或 MCP server。
- 实现 Agent loop 或业务编排。

### 2. Frontend State Layer

负责 UI 状态和 Runtime event 映射。

建议包含：

```text
taskStore
runStore
permissionStore
settingsStore
uiStore
devStore
```

负责：

- 保存当前 UI 选择、筛选、展开状态。
- 消费 `RuntimeEvent` 并更新任务、时间线和 Inspector。
- 调用封装后的前端 API client。

不负责：

- 直接处理后端业务规则。
- 修改后端 DTO shape。
- 绕过 API contract 读取 Runtime 内部状态。

### 3. Client API / Transport Layer

负责 Web 前端与后端 / 本地 Runtime 之间的安全、结构化通信。当前 Web-first 阶段优先实现 API client、event stream client 和本地 transport adapter；后续桌面端阶段再增加 preload / IPC adapter。

包括：

```text
api client
event stream client
transport adapter
typed request / response wrapper
desktop preload / ipc adapter later
```

负责：

- 暴露类型安全、最小化的前端调用 API。
- 执行 request / response 包装。
- 订阅 Runtime event stream。
- 阻止前端直接访问 Node、本地系统和数据库。
- 为后续桌面端保留 preload / IPC adapter 扩展点。

不负责：

- 业务编排。
- Agent loop。
- 具体工具执行。

### 4. Go Gateway / Runtime Orchestrator Layer

负责接收 Web API / transport 请求，做入口级校验、统一返回结构、AgentRun 入队、worker 调度、运行命令路由和事件扇出。

建议包含：

```text
TaskController
RunController
PermissionController
SettingsController
McpController
RedisRuntimeBus
WorkerManager
RunScheduler
EventStreamProxy
ErrorMapper
```

负责：

- 校验 request DTO。
- 将 AgentRun 入队到 Redis Runtime Bus。
- 路由 pause / resume / cancel / retry / permission decision 等 worker command。
- 返回统一 `ApiResult`。
- 把内部错误转换成 `AppError`。
- 消费 Redis runtime event stream，并扇出给 Web UI。
- 提供 worker 状态、健康检查和运行诊断入口。
- 管理 worker 心跳、并发、背压、超时、重试、dead letter、日志和基础会话边界。

不负责：

- 直接调用模型 provider。
- 直接执行工具或 MCP。
- 跑 LangGraph Agent loop。
- 把 Redis 状态当成 Task / AgentRun / ExecutionStep 的最终状态真相。
- 替代 Python PermissionManager 的核心决策。

#### 跨进程 Runtime Bus 配置（2B-2b）

Go Gateway 通过环境变量选择 runtime bus 实现。配置读取集中在 `apps/gateway/internal/orchestrator/factory.go`，
不散落在 handler 或 UI 层。Python Control Plane、Agent Worker 和 RAG Worker 必须消费相同的 Redis 地址、
密码和 logical DB；不可只给 Gateway 设置 `JARVIS_REDIS_DB`。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `JARVIS_RUNTIME_BUS` | `redis` | `redis`（默认生产链路）或显式测试用 `inmemory`，非法值启动失败 |
| `JARVIS_REDIS_ADDR` | `127.0.0.1:6379` | Redis 地址（仅 redis 模式） |
| `JARVIS_REDIS_PASSWORD` | （空） | Redis 密码（可选） |
| `JARVIS_REDIS_DB` | `0` | Redis DB 编号；所有 Runtime 进程必须一致，非法值统一回退 0 |

**MVP 持久化与 Control Plane 配置：**

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `JARVIS_DATABASE_URL` | 无 | Python Control Plane / Worker 使用的 PostgreSQL asyncpg DSN；缺失时拒绝启动 |
| `JARVIS_CONTROL_PLANE_URL` | 无 | Go Gateway 调用 Python Control Plane 的地址；Redis 模式下必填 |
| `JARVIS_ARTIFACT_ROOT` | `<项目根目录>/.local/artifacts` | Worker 与 Control Plane 共享的受控产物目录 |
| `JARVIS_ARTIFACT_INLINE_MAX_BYTES` | `8192` | 文本 Artifact 进入文件存储的 UTF-8 字节阈值 |
| `JARVIS_ARTIFACT_MAX_FILE_BYTES` | `52428800` | 单个 Artifact 上限，配置范围 1 KiB–100 MiB |
| `JARVIS_ARTIFACT_MAX_RUN_BYTES` | `262144000` | 单 Run Artifact 总量，配置范围 1 KiB–10 GiB |
| `JARVIS_ARTIFACT_MAX_WORKSPACE_BYTES` | `2147483648` | 单 Workspace Artifact 总量，配置范围 1 KiB–100 GiB |
| `JARVIS_ARTIFACT_MAX_TOTAL_BYTES` | `10737418240` | Artifact 根目录总量，配置范围 1 KiB–500 GiB |
| `JARVIS_RAG_ASSET_MAX_FILE_BYTES` | `16777216` | 单个 RAG Asset 上限，配置范围 1 KiB–100 MiB |
| `JARVIS_RAG_ASSET_MAX_TOTAL_BYTES` | `21474836480` | RAG Asset 根目录总量，配置范围 1 KiB–500 GiB |
| `JARVIS_RAG_JOB_LEASE_SECONDS` | `300` | RAG ingestion/embedding Job lease，配置范围 5–1,800 秒 |
| `JARVIS_TEST_FAULT_INJECTION_ENABLED` | `false` | 仅隔离故障验收可启用；生产和普通开发保持关闭 |
| `JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT` | （空） | 获批工具进入 executor 前的测试屏障绝对目录；设置时必须同时启用测试总开关 |
| `JARVIS_TEST_TOOL_EFFECT_BARRIER_TIMEOUT_SECONDS` | `120` | 等待同名 `.release` 文件的时间，范围 1–600 秒；超时 fail closed |

Artifact 四级配置必须满足 `file <= run <= workspace <= total`，RAG Asset 必须满足
`file <= total`，否则进程拒绝启动。两个本地 Store 都在跨进程锁内完成有界用量检查和原子替换；
容量统计最多检查 100,000 个目录条目，达到扫描预算时 fail closed，避免容量保护自身成为无界工作。
测试故障注入配置采用成对校验：只设置屏障目录、只打开总开关、使用相对目录或非法超时都会拒绝 Worker
启动。屏障仅用于专用 Workspace/数据库，不得写入日常工作目录，也不得作为生产暂停机制。
总开关启用后，同一屏障目录还可放置固定文件 `model-recoverable-failure.trigger`：Worker 的下一次
ModelProvider 入口会将其原子改名为 `model-recoverable-failure.consumed`，并注入一次
`MODEL_TIMEOUT / recoverable=true`。没有 trigger 时模型行为不变；每次注入必须由测试驱动显式重新创建
trigger。该入口只用于隔离 REC-05，不接受用户 prompt、工具参数或远端请求触发。

**应用日志配置：**

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `JARVIS_LOG_DIR` | `<项目根目录>/.local/logs` | 日志文件目录 |
| `JARVIS_INSTANCE_ID` | 服务默认值 | 实例 ID |
| `LOG_LEVEL` | `INFO` | 日志级别：DEBUG / INFO / WARN / ERROR |
| `JARVIS_LOG_COLOR` | `auto` | 终端颜色模式：`auto` / `always` / `never`；`NO_COLOR` 优先级更高 |
| `NO_COLOR` | 空 | 设置后禁用终端颜色 |

日志同时输出到彩色终端（stderr）和滚动文件（`.local/logs/`）。
详细格式与规范参见 `docs/18-observability-logging-design.md`。

启动方式：

```bash
# 首次安装或依赖变化后执行
scripts/dev.sh setup

# 完整开发链路（项目根目录，推荐）
scripts/dev.sh

# 以下仅用于 Gateway adapter 隔离调试
cd apps/gateway
JARVIS_RUNTIME_BUS=inmemory go run ./cmd/gateway

# 自定义 Redis 地址
JARVIS_RUNTIME_BUS=redis \
JARVIS_REDIS_ADDR=10.0.0.1:6380 \
JARVIS_CONTROL_PLANE_URL=http://127.0.0.1:8100 \
go run ./cmd/gateway
```

#### Event Pump（2B-2c）

redis 模式下 Gateway 启动后台 event pump，从 `jarvis:stream:runtime-event` 读取 worker 事件并写入 in-memory store，使 SSE 能读取到外部事件。

**启动流程：**

```text
internal/app/app.go
  → orchestrator.NewRuntimeBus(cfg) // inmemory: pump=nil; redis: pump!=nil
  → pump.Start()                 // 幂等创建 consumer group + 启动 goroutine
  → defer pump.Close()           // 进程退出时取消 context + 等待 goroutine
```

**事件流：**

```text
Redis StreamRuntimeEvent
  → stale PEL: XPENDING + XCLAIM / new: XReadGroup ">"
  → RuntimeEventReader.ReadDeliveries (逐条解码 + 校验)
  → InMemoryRuntimeBus.AppendRuntimeEvents (event id 幂等追加)
  → 每条成功后单独 XAck
  → GetEvents(runID) / SSE SubscribeEvents 可见
```

**运行时行为：**

- 非阻塞轮询，单次最多 32 条消息
- 读取失败：指数退避 100ms→200ms→400ms→...→5s，日志记录
- 空读取：50ms 间隔（避免 tight loop）
- consumer group 在 pump 启动时幂等创建（`XGroupCreateMkStream` + BUSYGROUP 处理）
- 进程退出：`signal.NotifyContext` 收到 `SIGINT`/`SIGTERM` → `server.Shutdown(ctx)` → `pump.Close()`

**SSE 持续推送：**

- `SubscribeEvents` 两阶段：先发初始快照，再用 300ms ticker 轮询 `GetEvents` 推送新增事件
- 基于 `event.id` 去重，不重复发送
- 客户端断开（`r.Context().Done()`）时退出
- 不再无条件把 run 标为 `completed`

**开发注意：**

- inmemory 模式下 pump 为 nil，不启动 goroutine，无需 Redis
- 切片 3A 已提供 Python agent-worker（`apps/agent-worker/`），可写入 Redis runtime event stream
- pump 只做读取/校验/缓存，不生成 mock worker outcome
- 事件追加使用 `AppendRuntimeEvents`，深拷贝保证线程安全
- 如需在本地启动 Redis 测试 pump：`redis-server` → `JARVIS_RUNTIME_BUS=redis go run ./cmd/gateway`

**测试：**

```bash
# 所有测试不依赖真实 Redis（使用 fake reader/client/backoff/fakeBus）
cd apps/gateway && go test -count=1 ./internal/orchestrator/ ./internal/redis/ ./internal/api/handlers/
```

### 5. Redis Runtime Bus Layer

负责 Go Orchestrator 与 Python worker pool 之间的跨进程运行时通信。

建议包含：

```text
RunQueue
WorkerCommandStream
RuntimeEventStream
WorkerHeartbeat
PendingPermissionSignal
DeadLetterQueue
```

负责：

- 承载待执行 AgentRun。
- 承载 pause / resume / cancel / retry / permission decision 等运行命令。
- 承载 RuntimeEvent。
- 暴露 worker 心跳、状态、负载和能力标签。
- 支持 pending、retry、dead letter 和 backpressure。

不负责：

- 成为业务数据库。
- 保存 Task / AgentRun / ExecutionStep / ToolCall / Permission / AuditLog 的最终状态。
- 执行 Agent loop、工具或模型调用。
- 替代 Storage Layer。

### 6. Python Agent Worker Runtime Layer

负责 Harness Agent 的核心运行。Python worker 从 Redis 消费 run job 和 command，执行 Agent loop，并把状态、审计和事件写回 Storage / Redis。

#### 3A Agent Worker（已实现，Phase 6B-1 增强）

`apps/agent-worker/` 提供 Python agent-worker。生产运行使用真实供应商 Provider（当前为
`deepseek`）或显式 `custom_openai_compatible`；mock provider/runner 只允许由自动化测试直接注入。

**本地密钥配置：**

```bash
cp apps/agent-worker/.env.example apps/agent-worker/.env
# 编辑 .env，填入真实值。.env.example 不包含真实密钥。
# .env 已被 .gitignore 忽略。外部环境变量优先于 .env。
```

**快速启动：**

```bash
conda activate jarvis-assistant
cd apps/agent-worker
pip install -e ".[dev]"

# DeepSeek 生产模式启动
JARVIS_MODEL_PROVIDER=deepseek \
JARVIS_MODEL_BASE_URL=https://api.example.com/v1 \
JARVIS_MODEL_NAME=<model-name> \
JARVIS_MODEL_API_KEY_ENV=MY_API_KEY \
MY_API_KEY=sk-... \
JARVIS_WORKSPACE_ROOT=/path/to/your/project \
python -m jarvis_worker.main
```

#### 独立 RAG Worker

RAG 建库不在 Agent Worker 中执行。它使用独立进程轮询 PostgreSQL `rag_ingestion_jobs`，顺序执行
PyMuPDF/PaddleOCR-VL 预处理、分块、OpenAI Embedding 与 pgvector 入库：

```bash
cd apps/agent-worker
JARVIS_RAG_STRUCTURE_PROVIDER=paddleocr-vl \
JARVIS_RAG_MLX_VLM_URL=http://127.0.0.1:8111/ \
JARVIS_RAG_STRUCTURE_CACHE_ROOT=/path/to/rebuildable/cache \
python -m jarvis_worker.agent.rag.worker
```

`scripts/dev.sh` 会统一启动和停止该进程；本地 MLX-VLM 可用时默认选择 `paddleocr-vl`，否则明确
选择 `native-only`。显式要求 `paddleocr-vl` 但本地 VLM 未启用时，启动脚本直接失败，不静默降级。
RAG Worker 不读取 `JARVIS_MODEL_*`，其 ID、轮询、资源目录、视觉 Provider 和 Embedding 配置全部
使用 `JARVIS_RAG_*`。OpenAI Key 仍只通过 `JARVIS_RAG_EMBEDDING_API_KEY_ENV` 指向的环境变量读取。
`JARVIS_RAG_STRUCTURE_CACHE_ROOT` 可选；默认使用 Workspace 下的 `.local/rag-cache/structure`。缓存按
PDF 内容、页/区域、DPI、路由策略和 Provider 版本寻址，只用于复用昂贵的本地视觉推理。修改解析策略或
模型版本会自然产生新键；缓存损坏按 miss 处理，不得把它当作 Job 状态、审计或可检索数据真源。

在线检索不进入 RAG Worker。Agent Worker 在启动时装配 L0 `rag.search`，其 executor 只把可信
`task_id` 与模型提供的 query/document_ids/top_k 交给 `RagRetrievalService`。Workspace 从 Task
持久化记录回查，禁止出现在模型可写参数中。可单独执行真实数据库安全 smoke：

检索算法通过 `RagRetrievalPipeline` 装配，阶段顺序固定为
`RagQueryRewriter -> RagCandidateRetriever -> RagReranker -> RagContextAssembler`。默认 Candidate
Retriever 已组合 pgvector、PostgreSQL 关键词查询和 RRF。默认生产重排顺序是
`HardFilter -> FeatureReranker -> 可选 SemanticReranker -> QuotaAwareMmrReranker -> PolicySelector`。
Reranker 通过独立 `RerankerProvider` 接入 Cross-Encoder；本地 adapter 使用无凭据 localhost HTTP
访问独立 BGE sidecar，默认模型为 `BAAI/bge-reranker-v2-m3`。未启用 Provider 时仍运行确定性
HardFilter/Feature/MMR/Policy 链路，不产生隐藏模型调用或模型下载。新增专用
BM25、具体 Reranker Provider 或 Query Rewrite 时应实现并替换对应端口，不得修改 `rag.search` Tool 参数、
绕过 Repository，或移除 Pipeline 的 Workspace/候选集合边界校验。RAG ToolResult 进入 Prompt 前
必须有界；模型只提交 citation chunk_id，最终来源信息由 Runtime 校验并渲染。

pgvector 语义召回由 `HnswSearchConfig` 统一拥有查询期参数，默认
`ef_search=100`、`iterative_scan=relaxed_order`、`max_scan_tuples=20000`、
`scan_mem_multiplier=1`。对应环境变量为 `JARVIS_RAG_HNSW_EF_SEARCH`、
`JARVIS_RAG_HNSW_ITERATIVE_SCAN`、`JARVIS_RAG_HNSW_MAX_SCAN_TUPLES` 和
`JARVIS_RAG_HNSW_SCAN_MEM_MULTIPLIER`。Repository 必须通过事务级 `set_config(..., true)` 应用，
禁止使用 session/global `SET` 污染连接池。SQL 只按 cosine distance 排序；候选回到
`PgVectorCandidateRetriever` 后再按 score/chunk_id 稳定排序。

运行前需在当前 shell 注入 `JARVIS_DATABASE_URL`；`dev.sh` 的默认值只会传给其启动的子进程。

```bash
cd apps/agent-worker
python tests/integration/rag_retrieval_postgres_smoke.py
# 同时验证真实 OpenAI Query Embedding
python tests/integration/rag_retrieval_postgres_smoke.py --real-embedding
# 只读确认 cosine HNSW 索引可进入 KNN 执行计划
python tests/integration/rag_hnsw_plan_smoke.py
```

该 smoke 的 fixture 全部位于回滚事务，必须验证跨 Workspace、非 ready 和 provider/model 不匹配
结果不会进入召回结果。HNSW plan smoke 使用已有真实向量且不写 fixture；小规模数据下默认规划器可能
合理选择顺序扫描，因此脚本只在回滚事务内禁用顺序扫描来验证索引可用性，不把强制计划当成生产性能。

#### RAG 质量评估

RAG 质量评估位于 `apps/agent-worker/eval/`，与 pytest 的代码正确性测试分离。正式主评测必须执行
`PDF -> PyMuPDF -> 页面路由 -> PaddleOCR-VL -> 融合 -> Chunk -> Embedding -> Retrieval ->
Generation`，并保存每阶段中间产物；native-only 只能作为路由前诊断对照。

重型视觉阶段使用 `.local/rag-runtimes/paddleocr-client`，可通过
`eval/runners/run_pipeline_eval.py` 运行。修改金标或评分算法后使用 `rescore_pipeline_eval.py` 复用缓存
产物；Embedding 及下游使用 `continue_pipeline_eval.py` 续跑，避免重复视觉推理。任何 provider 或
上游阶段失败都必须显式阻断下游，不允许自动退化。

生产 retrieval/reranker/context assembly 已由 `RagRetrievalService` 实现。每次真实 `rag.search`
成功后，`RagEvaluationTraceService` 在独立事务中保存不含 Chunk 正文和向量的阶段快照；采集失败只让
评估可观察性降级，不改变只读检索结果。ToolResult 返回 `evaluation_trace_id` 供后续反馈关联，但内部
candidate/reranker 排序不进入模型 observation。
Reranker 的 applied/degraded/skipped、Provider/模型、耗时、输入输出数量和安全 failure code 保存在
Pipeline trace；生产飞轮将紧凑执行摘要持久化到 `pipeline_versions`，不保存 Chunk 正文或原始异常。

本地 BGE 运行时需要显式安装；安装阶段会显示依赖与 Hugging Face 模型下载进度，并将模型预取到
本机缓存。完成后 `dev.sh` 的 `auto` 模式会启动 sidecar，并向 Agent Worker 注入本地 Provider：

```bash
scripts/rag/setup-bge-reranker.sh
scripts/dev.sh
```

可使用 `JARVIS_LOCAL_RERANKER_ENABLED=false` 强制禁用。sidecar 只监听 loopback，输入最多 30 条、
单条正文最多 6000 字符；模型故障通过 trace 降级，不得中断 RAG 查询。

轨迹初始 `privacy_status=pending`。只有隐私状态为 `approved`，且标签状态为
`confirmed/promoted` 的真实轨迹，才允许进入 `eval/framework` 计算 Candidate Recall/Precision/MRR/
nDCG、难负例侵入、Reranker delta/evidence drop 和 Context evidence recall，并生成待人工复核的失败
候选。文件 runner 的内存 cosine 与 OpenAI-compatible generation adapter 仍只用于历史 corpus 实验；
RAG generation owner 落地前必须保持 `production_chain_complete=false`。

内部人工复核使用 `eval/runners/review_production_traces.py`，不新增面向用户的反馈 API/UI。命令按
`list -> inspect -> documents/chunks -> approve|reject -> label -> evaluate -> promote` 执行。`list`
只展示 hash、版本和数量；`inspect/chunks` 是开发者显式读取本地 Query/Chunk 预览的动作。Label 中的
所有 Chunk 必须由 Application Service 验证属于 trace 的 Workspace；Candidate 漏召回时允许从同一
Workspace 文档中选择未进入排序的正确 Chunk。晋升只导出本地、无 Chunk 正文的回归候选，不直接改写
已有公开 corpus 或金标。

首批真实链路基线使用 `eval/tasks/production-rag-p0-v1.json` 与
`eval/runners/run_production_p0.py`。Runner 必须通过 Gateway 创建真实 Task，并把终态失败与成功一起
保存到本地报告；不得为了得到分数而直接调用 Retrieval Service。运行报告中的 Task/Run 血缘用于和
生产 trace 对齐，人工确认正确证据后再进入分阶段指标。未产生 trace 的失败仍计入链路执行结果，但不
伪造 Retrieval 标签。

**配置：**

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `JARVIS_REDIS_ADDR` | `127.0.0.1:6379` | Redis 地址 |
| `JARVIS_WORKER_ID` | `worker-01` | Worker 标识 |
| `JARVIS_WORKER_GROUP` | `jarvis:group:worker-pool` | Consumer group |
| `JARVIS_WORKER_CONSUMER` | 同 `JARVIS_WORKER_ID` | Consumer 名称；多 Worker 必须唯一 |
| `JARVIS_RUN_QUEUE_RECLAIM_IDLE_MS` | `65000` | stale RunJob 首次可接管时间，最小 65 秒 |
| `JARVIS_RUN_QUEUE_RECLAIM_INTERVAL_MS` | `5000` | Worker 扫描 PEL 的间隔 |
| `JARVIS_RUN_QUEUE_MAX_DELIVERIES` | `3` | RunJob 最大交付次数，超限进入 DLQ |
| `JARVIS_COMMAND_RECLAIM_IDLE_MS` | `5000` | worker command 首次可接管时间，最低 1 秒 |
| `JARVIS_COMMAND_RECLAIM_INTERVAL_MS` | `1000` | Worker 扫描 command PEL 的间隔 |
| `JARVIS_GATEWAY_ID` | hostname + pid | Gateway Redis consumer name；多实例必须唯一 |
| `JARVIS_WORKSPACE_ROOT` | 空 | 默认 workspace 根目录（ToolGateway MVP），缺失时 list_files 返回 WORKSPACE_ROOT_REQUIRED |
| `JARVIS_ALLOWED_WORKSPACE_PATHS` | 空 | Control Plane 启动时幂等注册为 configured Workspace 的路径列表（使用系统 PATH 分隔符） |
| `JARVIS_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Gateway 允许的 Web Origin 白名单，逗号分隔；未知 Origin 返回 403 |
| `JARVIS_MODEL_PROVIDER` | `deepseek` | `deepseek` 或 `custom_openai_compatible`；旧值仅用于迁移 |

Workspace 开发约定：

- Web 通过 `GET /api/workspaces` 消费 PostgreSQL Registry，不再从 Settings 的 allowed path 列表构造选择状态。
- `POST /api/workspaces/pick` 会在 macOS 弹出真实系统目录选择器；自动化测试必须注入 fake/subprocess double，不得弹真实窗口。
- picker 请求使用独立长超时，普通 Control Plane 请求保持短超时；请求取消后必须终止 osascript 并继续传播 cancellation。
- configured Workspace 由环境配置管理，不允许 Web revoke；picker Workspace 可在 Settings 中二次确认撤销。

**端到端运行（需 Redis）：**

本地开发不提供 Worker 代码热重载。修改 Python Worker、Skill 包、工具注册或 Prompt 后，必须先
完整停止当前服务栈，再重新启动一次；不得让旧 Worker 与新 Worker 使用相同的
`JARVIS_WORKER_ID` / consumer 名称同时消费队列。该约定是本地开发操作流程，不引入运行时
重复进程检测或自动修复机制。

```bash
# 项目根目录；同时启动 PostgreSQL、Redis、Control Plane、Gateway、Agent/RAG Worker、Web
JARVIS_WORKSPACE_ROOT=/path/to/your/project scripts/dev.sh

# 另一个终端创建任务
curl -X POST http://localhost:8080/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"user_goal": "测试任务"}'

# SSE 观察事件
curl http://localhost:8080/api/runs/<run_id>/events
```

**测试：**

```bash
cd apps/agent-worker

# 本地开发默认使用 jarvis-assistant conda 环境
conda activate jarvis-assistant
pip install -e ".[dev]"

# 运行测试（使用 fakeredis，不依赖真实 Redis）
conda run -n jarvis-assistant python -m pytest -q   # 144 tests
```

Python 静态检查与格式验收统一使用项目 `pyproject.toml` 中的 Ruff 配置：

```bash
../../scripts/check-python-quality.sh
```

Ruff 属于 `dev` 可选依赖，不需要启动 PostgreSQL、Redis、Gateway、Worker 或 Web。脚本对历史代码
执行语法、未定义名称等高风险基线，对 Git 中当前新增/修改的 Python 文件执行 import、Pyflakes 和
代码结构严格门，新文件还必须通过格式检查。新增 Python 代码在提交前必须同时通过 Ruff、
compileall 和相关 pytest；不得用全局 ignore 掩盖本次新增问题。

`pyproject.toml` 是 agent-worker 的依赖真源；`jarvis-assistant` conda 环境是本机开发环境。
本地测试和运行默认使用该 conda 环境，避免误用系统 Python 或 Homebrew Python。
服务器部署时可继续复用同一份 `pyproject.toml`，在 venv、容器或 CI 环境中执行 `pip install .`。

**3A 已知债务：**

- **Pending message recovery 未实现**：`RunQueueConsumer` 只用 `XREADGROUP streams={run_queue: ">"}` 读取新消息，不处理 consumer group 中已有的 pending messages。处理失败时消息不 ack 会进入 pending，但 worker 重启后不会自动 reclaim / retry pending。后续 WorkerManager 切片需要实现 XPENDING / XAUTOCLAIM / retry / dead-letter 策略。Redis 仍不是业务真源，最终恢复状态需与 Storage 对齐。

**3B Worker Heartbeat 验证方式：**

```bash
# Terminal 1: 启动完整链路
JARVIS_WORKSPACE_ROOT=/path/to/your/project scripts/dev.sh

# Terminal 2: 验证 worker status API
curl http://localhost:8080/api/runtime/workers

# 查看 worker 状态是否从 starting → idle
# 再创建任务：
curl -X POST http://localhost:8080/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"user_goal": "测试"}'

# 状态应变为 busy（active_run_id 非空）
curl http://localhost:8080/api/runtime/workers
```

**3B 测试命令：**

```bash
# Python
cd apps/agent-worker
conda run -n jarvis-assistant python -m pytest -q   # 144 tests

# Go
cd apps/gateway
go test ./...                                       # 全部通过
go vet ./...                                        # 无输出

# Web
cd packages/shared && npm run typecheck
cd apps/web && npm run build
```

**3C Worker Command / Cancel 验证方式：**

```bash
# 启动完整链路后，取消运行：
# 1. 创建任务
curl -X POST http://localhost:8080/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"user_goal": "长时间任务"}'

# 2. 取消运行（run_id 从上一步获取）
curl -X POST http://localhost:8080/api/runs/<run_id>/cancel

# 3. 查看 SSE 事件流，应收到 agent.run.cancelled
# Timeline 显示 cancelled，composer 恢复可用
```

**3C 测试命令：**

```bash
# Python（含 10 个 cancel 测试）
cd apps/agent-worker
conda run -n jarvis-assistant python -m pytest -q   # 144 tests

# Go（含 4 个 RunCancelCommand 测试）
cd apps/gateway
go test ./...                                       # 全部通过
go vet ./...                                        # 无输出
```

包括：

```text
WorkerEntrypoint
RunConsumer
CommandHandler
AgentRunner
Planner
SkillLayer
ContextManager
MemoryManager
ModelRouter
ToolGateway
PermissionManager
LangChainAdapters
LangGraphRuntime
EventBus
MultiAgentOrchestrator
ErrorHandler
```

### Skill 开发边界

运行时代码位于 `apps/agent-worker/src/jarvis_worker/agent/skills/`，与 `agent/tools/` 同级；
可安装的通用 Skill 包位于仓库根目录 `skills/<skill-id>/`。包只强制包含标准 `SKILL.md`，可按需
包含 `agents/openai.yaml`、`references/`、`assets/`、`schemas/` 和 `scripts/`。Jarvis 产品适配配置
独立位于 `skills/.jarvis/<skill-id>.json`，也可通过 `JARVIS_SKILL_ADAPTERS_ROOT` 指向其他受信任目录；
标准 Skill 包不得为了接入 Jarvis 而把具体 Tool、权限、激活或调度策略写入 `SKILL.md`。

- Skill 只描述宿主无关的领域方法，不复制 Tool executor 的执行逻辑，也不声明产品工作流、证据工具、
  权限或持久化生命周期。
- Jarvis adapter 负责激活、渐进资源、脚本注册和资源上限，不负责把 capability role 映射成固定业务
  阶段。SkillLoader 必须分别校验包路径与 adapter 路径，并把两者共同纳入 fingerprint。
- AgentRunner 只能把 Skill 交给 ContextManager，不能直接运行包内脚本。
- 包内脚本默认不可执行；显式 `execution_enabled=true` 时，由启动装配自动注册为
  `skill.<skill-id>.<script-name>` ToolManifest，并且只能通过通用 SkillScriptExecutor、
  ToolGateway、PermissionManager、AuditLog 和 RuntimeEvent 执行。
- v1 只支持固定 Python runtime、固定 argv、JSON object 输入输出、L1、禁网、无继承 secret
  环境变量、1–30 秒超时、输入/输出大小上限和启动时脚本哈希。它不是不可信代码的 OS 沙箱，
  因此 Skill 安装目录仍是受信任代码边界，不允许用户上传任意脚本即执行。
- Skill 脚本只能做确定性计算或校验；不得写 Vault、数据库、Workspace，不得调用 MCP 或派生
  子进程。需要副作用时必须返回结构化结果，再调用独立的受权限工具。
- 外部来源正文始终是不可信数据，不能因进入 Skill reference 或知识笔记而提升为系统指令。

### 通用 Skill 安装与兼容约束（后置设计）

当前 `SkillLoader` 只加载已经进入受信任 `skills/` 根目录的包，并通过独立 Jarvis adapter 完成激活、
渐进引用、能力绑定和脚本注册。它不是下载器、包管理器、恶意内容扫描器或第三方代码沙箱。现阶段
不得把网络下载、用户上传或未知来源的 Skill 直接复制到该目录。

后续支持通用 Skill 导入时，职责必须拆分为：

```text
SkillInstallService
-> quarantine/staging package validation
-> SkillPackageLoader（只理解标准 Skill 包）
-> JarvisSkillAdapterResolver（解析宿主能力与依赖）
-> compatibility / trust decision
-> user capability review
-> atomic activation into trusted registry
-> SkillLayer
```

- `SkillPackageLoader` 只解析标准 `SKILL.md`、直接引用的有界文本资源和 UI metadata，不理解 Jarvis
  Tool、权限或调度策略。
- `JarvisSkillAdapterResolver` 优先使用受信任 adapter；没有 adapter 时只能产生安全降级结果，不能
  猜测写入、网络、文件、Memory、调度或脚本权限。
- 纯指令型通用 Skill 未来可进入 `restricted`：仅显式调用、无工具、无脚本、无后台执行。需要 native
  Tool、MCP、脚本、定时任务、异步恢复或持久化的 Skill，必须完成依赖解析或显示为
  `needs_dependencies/needs_adapter`。
- 可选第三方包无效时应隔离该 Skill 并保留结构化错误，不应使整个 Worker 无法启动；内置必需 Skill
  和受信任 adapter 仍保持启动期 fail closed。
- 安装必须从隔离目录原子提升到正式目录，不允许 Loader 观察半安装状态。正式版本固定 package hash、
  adapter hash、来源、版本和用户批准的能力摘要；更新后重新审核。
- Web 未来展示 `available`、`available_limited`、`needs_dependencies`、`needs_adapter`、`disabled`、
  `invalid`、`quarantined`，并说明发布者、来源、fingerprint、脚本、网络、本地读取、写入和后台
  能力请求。

上述安装和兼容层当前只作为设计约束记录，不进入本轮实现，也不意味着当前 MVP 引入插件市场。

负责：

- 消费 Redis run queue / command stream。
- 执行 Agent loop。
- 构造上下文。
- 调用模型。
- 让 Agent 自主选择工具并发起动作。
- 通过 ToolGateway 和 PermissionManager 约束动作边界。
- 发布 Runtime events。
- 处理失败、重试、暂停、恢复。
- 使用 LangGraph 编排 loop、状态图、human-in-the-loop 和 multi-agent graph。
- 使用 LangChain 封装模型、prompt、tool wrapper、retriever 和 parser。
- 将 LangGraph 状态同步为项目 Task / Run / Step / Event。

不负责：

- UI 展示。
- Web API / transport DTO 解析。
- 全局 worker 调度和背压策略。
- 具体数据库实现。
- 具体 MCP SDK 或系统 API 细节。
- Go Gateway 的前端适配和错误包装。

### 7. Capability Adapter Layer

负责连接外部能力和本地能力。

包括：

```text
Model Providers
Native Tools
MCP Adapter
Local System Bridge
File Tool
Shell Tool
Browser Tool
Clipboard Tool
Notification Tool
```

负责：

- 适配模型 provider。
- 适配 native tool。
- 适配 MCP server。
- 适配 macOS、本地文件、Shell、浏览器等能力。
- 把外部响应转换为内部统一结果。

不负责：

- 决定是否允许执行动作。
- 绕过 ToolGateway 执行动作。
- 直接更新 UI。

#### Workspace native tool 模块组织

Workspace 能力在自己的 domain package 内同时拥有声明、执行器与共享安全策略：

```text
agent/tools/workspace/
├── module.py            # ToolManifest、Prompt metadata 与 executor bindings
├── __init__.py          # 只导出稳定 executor
├── path_policy.py       # workspace 边界、路径规范化、dir-fd/symlink 策略
├── list_files.py
├── get_file_info.py
├── search_files.py
├── read_file.py
├── create_file.py
├── create_directory.py
├── move_path.py
└── delete_path.py
```

- `tools/__init__.py` 不拥有工具业务实现；`module.py` 从同一 capability 的稳定 facade
  绑定 executor，bootstrap 不逐个导入或注册 Workspace 工具。
- 跨工具路径安全逻辑只能由 `path_policy.py` 拥有；各 executor 不复制另一套边界或 symlink 规则。
- 每个工具模块拥有自己的输入语义、资源上限、结果和错误映射；新增工具不得继续堆入单体文件。
- 写入结构工具必须声明明确的覆盖/递归语义：创建目录不补建父目录，移动不得覆盖目标，删除只允许文件、符号链接或空目录。无法提供这些安全语义的平台必须 fail closed。

#### Capability module 装配规则

```text
agent/tools/<domain>/module.py
  -> CapabilityModule(ToolManifest + executor bindings)
  -> agent/tools/builtin.py 显式启用
  -> agent/tool_gateway/catalog.py 预检重复 id / tool name
  -> ToolRegistry（唯一注册中心）
  -> ToolGateway -> PermissionManager -> executor
```

- 每个 domain module 拥有自己的工具名、schema、risk 和可选 Prompt metadata；PromptBuilder
  与 ActionParser 不得再维护独立工具清单。
- `CapabilityModule` 只声明绑定，不执行工具、不判断权限、不写 Storage/AuditLog/RuntimeEvent。
- 具体 executor、manifest 和领域共享策略必须位于所属 `agent/tools/<domain>/`；
  `agent/tool_gateway/` 只保留 gateway、registry、装配 catalog 和跨工具统一契约。
- `agent/tools/` 不得放 PermissionManager、Repository、数据库连接或 Runtime service；
  Permission policy 位于 `agent/permissions/`，ToolCall 持久化位于
  `runtime/tool_calls/`。
- `bootstrap/tool_registry.py` 只负责组装，不拥有任何具体工具 manifest。
- 新的 L0 工具仍需进入 `PermissionManager` 的显式安全白名单；模块化不能自动获得低风险放行。
- 当前只允许代码内显式装配，不做目录扫描、entry point discovery、插件市场或运行时热插拔。

### 8. Storage Access Layer

负责 Store / Repository 接口。

源码目录不再使用通用 `storage/` 聚合所有功能。Repository 接口和实现优先跟随所属
功能，例如 `agent/memory/repository.py` 与
`agent/memory/postgres_repository.py`；公共连接、ORM、UnitOfWork 和 Outbox/Inbox
位于 `database/`。这是物理目录调整，不改变 Storage Access Layer 的依赖约束。

建议包含：

```text
TaskStore
RunStore
StepStore
ToolCallStore
PermissionStore
AuditStore
ArtifactStore
ConversationStore
MemoryStore
SettingsStore
McpStore
EventStore
```

负责：

- 屏蔽具体数据库 backend。
- 提供稳定的读写接口。
- 支持任务恢复、时间线查询、审计查询和设置读取。

不负责：

- 业务决策。
- 权限判断。
- Agent loop。

### 9. Persistence Layer

负责真实持久化实现。

包括：

```text
Relational database backend
Local artifact file store
System Keychain / encrypted secret store
Optional vector store
Optional remote sync store
```

负责：

- 数据库连接和 migration。
- 文件 artifact 保存。
- 敏感凭证保存。
- 向量索引或远程同步的后续扩展。

不负责：

- 暴露给 Renderer。
- 承载业务规则。
- 被 Runtime 直接依赖。

## 越层调用限制

禁止：

```text
Frontend UI -> raw transport
Frontend UI -> database
Renderer -> fs / shell / MCP server
Go Gateway handler -> database client
Python Agent Worker -> concrete database client
Go Gateway / Runtime Orchestrator -> Agent loop
Go Gateway -> concrete database client
Go Gateway -> tool execution
Redis -> business truth
FastAPI request -> long-running AgentRun hot path
AgentRunner / LangGraph node -> raw SQL
AgentRunner / LangGraph node -> MCP server directly
Tool adapter -> Permission Dialog directly
Persistence -> business decision
```

允许：

```text
Frontend UI -> Frontend State
Frontend State -> typed API client
typed API client -> Go Gateway
Go Gateway -> Redis run queue / worker command stream
Python Agent Worker -> Runtime / Store interface
Python Agent Worker Runtime -> ToolGateway / Store interface / EventBus
ToolGateway -> PermissionManager -> Capability Adapter
Store interface -> Persistence adapter
Python Worker -> Redis runtime event stream -> Go Gateway event fan-out -> Frontend State
```

## 模块边界

### Web App

负责：

- 展示 UI。
- 收集用户输入。
- 展示任务状态。
- 展示 Agent 执行过程。
- 展示权限确认。
- 管理用户设置。

不负责：

- 直接执行 Shell。
- 直接读写任意文件。
- 直接调用 LLM。
- 直接处理 Agent 推理循环。

### Go Gateway / Runtime Orchestrator

负责：

- 接收前端 API 请求。
- 校验 DTO。
- 统一 `ApiResult` / `AppError`。
- AgentRun 入队。
- worker command 路由。
- worker 心跳、并发、取消、超时、重试和背压。
- RuntimeEvent 扇出。
- 暴露 Dev Console API。

不负责：

- 跑 LangGraph Agent loop。
- 直接执行工具。
- 直接调用 LLM。
- 拥有核心业务状态真相。
- 持久化核心业务数据。

### Desktop App Later

负责：

- 在 Web 端系统稳定后封装桌面 shell。
- 复用稳定后的 Web UI。
- 提供 preload / IPC adapter。
- 接入菜单栏、通知、快捷键和更深的桌面能力。

不负责：

- 改写共享 DTO 和 RuntimeEvent 语义。
- 绕过 ToolGateway 或 PermissionManager。
- 在当前 Web-first MVP 中提前成为主线。

### Agent Runtime

负责：

- 消费 Redis run queue / command stream。
- 运行 Agent loop。
- 保存运行状态。
- 发布运行事件。
- 协调 context、model、tool、permission。
- 通过 LangGraph 编排 loop 和多 Agent 图。
- 通过 LangChain 使用模型、prompt、retriever、parser 和 tool wrapper。

不负责：

- 具体 UI 展示。
- 具体模型供应商 SDK 细节。
- 具体本地系统 API 细节。
- 全局 worker 调度和背压策略。

### Model Router

负责：

- 根据任务选择模型。
- 调用模型 provider。
- 处理流式输出。
- 统一模型响应格式。
- 可通过 LangChain model wrapper 适配不同模型。

不负责：

- 任务规划逻辑。
- 工具执行。
- 权限判断。

### Context Manager

负责：

- 收集相关上下文。
- 压缩历史对话。
- 检索相关记忆。
- 构造 ContextPackage。

不负责：

- 判断工具权限。
- 调用本地系统工具。
- 展示上下文 UI。

### Tool Gateway

负责：

- 注册工具。
- 校验工具调用参数。
- 请求权限判断。
- 执行工具。
- 返回结构化结果。
- 写入工具调用日志。

不负责：

- 决定任务下一步。
- 生成自然语言最终回复。

### Permission Manager

负责：

- 判断工具调用风险。
- 匹配权限规则。
- 请求用户确认。
- 保存权限授予记录。

不负责：

- 执行工具本身。
- 构造模型 prompt。

### Storage

负责：

- 持久化任务。
- 持久化 AgentRun。
- 持久化 ExecutionStep。
- 持久化 ToolCall。
- 持久化 Settings。
- 持久化 AuditLog。

不负责：

- 业务决策。
- 权限判断。

## 数据流示例

### 创建任务

```text
Web App
-> API client createTask
-> Go Gateway validate and initialize
-> Storage insert or update Task / AgentRun through approved service boundary
-> Redis enqueue AgentRun
-> Python Worker consume run job
-> EventBus task.created
-> Redis runtime event stream
-> Go Gateway event fan-out
-> Web App update UI
```

### 执行工具

```text
AgentRunner
-> ToolGateway request tool call
-> PermissionManager check
-> EventBus permission.required if needed
-> Redis runtime event stream
-> Go Gateway event fan-out
-> Web App permission dialog if needed
-> Go Gateway resolvePermission
-> Redis worker command stream
-> ToolGateway execute
-> Storage save ToolCall
-> EventBus tool.call.finished
-> AgentRunner observe result
```

### 模型调用

```text
AgentRunner
-> ContextManager build ContextPackage
-> ModelRouter select provider
-> Provider stream response
-> EventBus model.delta
-> AgentRunner parse next action
```

## 开发优先级

建议按以下顺序开发：

```text
1. Storage interfaces and schema contract
2. EventBus
3. Web API / Go Gateway contract
4. Runtime command / event envelope
5. Redis Runtime Bus or in-memory bus implementation
6. Vue Web skeleton and Dev Console
7. Go Gateway / Runtime Orchestrator mock adapter
8. Python mock Agent Worker
9. TaskManager
10. ModelRouter mock provider via LangChain adapter
11. AgentRunner minimal loop via LangGraph
12. ToolGateway with mock tools
13. PermissionManager
14. Real file read tool
15. Real LLM provider
16. Agent timeline UI
```

## 测试策略

每个模块至少需要：

- 单元测试：核心逻辑。
- 集成测试：模块之间的数据流。
- 手动测试：Web UI、权限弹窗和运行事件展示。
- Gateway / Orchestrator 测试：DTO 校验、错误映射、入队、命令路由、事件扇出、worker 心跳和超时。
- Redis Bus 测试：queue、consumer group、event stream、幂等、retry、dead letter。
- Runtime 测试：LangGraph 状态流转、LangChain adapter、ToolGateway、PermissionManager。
- 回归用例：典型任务完整执行。

### RAG 离线质量评测

RAG 的真实文档评测系统位于与 `src/`、`tests/` 同级的 `apps/agent-worker/eval/`，不放入
`src/jarvis_worker/agent/` 生产包。`agent/rag` 仍是预处理、分块、Embedding、索引和检索的唯一
领域 owner；评测系统只消费其公开契约和输出，不拥有运行时业务状态。

单元测试验证确定性行为与错误边界，离线评测衡量真实文档上的解析保真度、阅读顺序、结构恢复、
多模态关系、分块语义完整性和来源定位。公开、私有、生成语料必须通过 case manifest 登记来源、
许可证、隐私等级、安全相对路径和 SHA-256；私有失败样本默认不进入 Git。只有经过人工复核的
`verified` 案例可以作为发布质量门，评测报告属于本地产物，不作为业务真源。评测按
preprocessing、chunking、embedding、retrieval、generation 和 end-to-end 分阶段执行；各阶段共享
语料和 query/evidence 金标但必须分别报告，避免最终回答分数掩盖上游退化。公开 PDF 通过固定 URL
和 SHA-256 重建到 Git 忽略的本地 cache，大型二进制不得直接进入普通 Git 历史。

MVP 核心回归用例：

```text
1. 用户输入任务，创建 Task。
2. Runtime 启动 AgentRun。
3. Agent 调用 mock model。
4. Agent 请求文件读取工具。
5. PermissionManager 判断是否允许。
6. ToolGateway 返回结果。
7. Agent 生成最终输出。
8. UI 显示完整时间线。
9. Storage Layer 中保存任务和步骤。
```

## MVP RC1 发布门禁

当前阶段的统一质量入口是：

```bash
scripts/release-gate.sh automated
```

它同时覆盖 Shared contract、Gateway、Web 和 Agent Worker，不替代真实网页验收。准备发布候选时还必须
启动完整 Runtime，完成 `scripts/release-gate.sh runtime`，并按
`docs/20-mvp-rc1-release-gate.md` 执行八条真实用户旅程、提交结构化证据。

代码测试、Runtime smoke、真实旅程证据必须来自同一 Git revision。单个模块测试通过、历史进度记录或
一次成功演示都不能单独作为 RC1 完成结论。

## RC2 工程发布门禁

P7 起，pull request 和 `main` push 的确定性入口统一为：

```bash
scripts/release-gate.sh ci
```

`ci` 与 `automated` 共用同一实现，结果目录同时包含 `summary.txt`、`steps.tsv` 和 `report.json`。
GitHub Actions 只执行不需要个人数据库、模型密钥或本地 Artifact 的确定性门禁。

准备 RC2 工程候选时，在已启动的完整 Runtime、干净工作区和显式 PostgreSQL 连接上执行：

```bash
JARVIS_DATABASE_URL='postgresql+asyncpg://...' scripts/release-gate.sh rc2
```

该命令串行执行代码门、Runtime smoke 和 promoted-only RAG 门。普通 `ci`、`runtime` 或 `rag` 单独通过
不能宣称 RC2 放行；只有干净工作区中的 `rc2` 报告可以标记 `release_candidate_eligible=true`。
详细契约见 `docs/27-p7-engineering-release-productization.md`。

## 首次启动 preflight

安装依赖并填写本地配置后，必须先执行：

```bash
scripts/dev.sh doctor
```

该入口调用生产配置 owner 校验模型与 RAG 设置，并检查系统命令、Conda/Node 依赖、Workspace 与产物目录、
可选本地 Runtime 和应用端口。输出同时写入 `.local/preflight/<UTC timestamp>/report.json`。

`warning` 只表示可选能力降级；`failed` 会使 doctor 和 `scripts/dev.sh start` 非零退出。报告只含稳定
检查 ID、安全摘要和修复建议，不包含环境变量值、密钥名、URL 凭据或本地绝对路径。

## 数据库升级开发约束

- schema 变化必须先新增单链 Alembic migration，并保持唯一 head；禁止直接改生产表或手工改
  `alembic_version`。
- `scripts/dev.sh start` 只执行 current 检查，不拥有 migration 决策。
- 本地升级统一执行 `scripts/data-lifecycle.py upgrade --confirm`。该入口会先备份并在隔离库恢复，对全部
  public 表进行集合和精确行数对账，验证通过后才执行 `alembic upgrade head`。
- `data-lifecycle.py` 默认从本地 `JARVIS_DATABASE_URL` 安全解析数据库身份，确保 `scripts/dev.sh start`
  检查的就是随后 Control Plane/Worker 使用的数据库；`JARVIS_DATA_DB_USER/SOURCE_DB/DB_PASSWORD` 仅作为
  显式覆盖。生命周期工具只管理 `127.0.0.1/localhost:5432` 的 Compose PostgreSQL，远端 DSN 必须拒绝，
  防止对错误数据库做备份、迁移或状态判定。
- `restore-drill` 和 `upgrade` 必须停应用；只有只读 status 和 PostgreSQL custom-format backup 允许在线。
- 自动入口不覆盖源数据库。真实灾难恢复属于高影响运维动作，必须单独确认目标库、停机窗口和回滚点。

## 诊断与支持包开发约束

- `scripts/runtime-support.py` 只消费 Gateway/Control Plane 已有安全 DTO 和本地日志聚合，不新增 raw
  Redis、SQL 或日志下载接口。
- 支持包必须使用显式成员白名单；新增成员前必须同步 P7 文档、容量限制、自检和敏感模式验收。
- 禁止打包原始日志、AuditLog、数据库备份、Artifact、RAG 文档、`.env`、用户输入、模型内容或工具参数。
- 日志诊断只允许按服务聚合级别、大小和代码调用位置，不输出原始消息、trace/task/run ID 或实际文件名。
- 所有读取必须有 timeout/bytes/files 上限；Gateway 不可用属于可诊断降级，不应阻止本地支持包生成。
- 支持包只在本地创建，不自动上传；任何外部发送都需要用户另行明确授权。

## RC2 候选证据约束

- `scripts/rc2-candidate.py` 是最终候选记录唯一入口，只消费结构化报告，不解析自然语言日志判定通过。
- RC2 gate、support bundle、data recovery、runtime fault 和 audit retention 必须来自当前同一干净 revision。
- 新增或删除候选必需门禁时，必须同步脚本 required sets、self-test、P7 文档和 release gate。
- 候选记录只保存安全聚合和 SHA-256，不复制业务 ID、输入输出、原始日志或本机路径。
- 候选记录不是发布授权；Git tag、Release、安装包、上传和外部分发必须由用户单独请求。
