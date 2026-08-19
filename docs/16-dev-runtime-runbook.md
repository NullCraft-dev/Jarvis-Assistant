# Dev Runtime Runbook

本文档固化当前 Web-first MVP 的本地开发启动与冒烟验收路径。它只描述现有运行链路，不引入新的架构职责。

当前主链路：

```text
Vue Web
-> Go Gateway / Runtime Orchestrator
-> Redis Runtime Bus
-> Python Agent Worker
-> Redis runtime event / heartbeat streams
-> Go Gateway EventPump / HeartbeatPump
-> SSE / worker status API
-> Vue Web
```

## 适用范围

本 runbook 用于验证以下能力是否处于可开发状态：

- Gateway 默认以 `JARVIS_RUNTIME_BUS=redis` 启动。
- Gateway 可以把 `POST /api/tasks` 转成 Redis run job。
- Python worker 可以消费 run job，通过真实 LLM + AgentRunner + ToolGateway 执行并发布 RuntimeEvent。
- Gateway event pump 可以把 worker event 扇出到 SSE。
- Python worker 可以发布 heartbeat，Gateway 可以通过 `/api/runtime/workers` 暴露 worker 状态。
- Vue Web 可以通过 API client / SSE 观察任务和运行状态。

不负责：

- LangGraph 图编排验收（当前是最小 AgentRunner loop）。
- 权限决策完整 worker resume 验收。
- 桌面端 / Electron / IPC 验收。
- Memory / 向量检索验收。

ToolGateway `workspace.list_files` / `workspace.read_file` 是 MVP 已实现能力。`JARVIS_WORKSPACE_ROOT` 定义默认工作区，`JARVIS_ALLOWED_WORKSPACE_PATHS` 定义 Web 可选择和 Control Plane 可接受的根目录集合。

## 一键启动（推荐）

首次运行先一键安装项目依赖：

```bash
scripts/dev.sh setup
```

`setup` 会创建或更新 `jarvis-assistant` Conda 环境，安装 Python Worker / Control Plane、Web、shared contracts、Go modules，并拉取 PostgreSQL / Redis images。如果 `.env` 不存在，它还会复制模板；随后需要编辑 `apps/agent-worker/.env`，填入真实模型配置和密钥。

安装完成后在项目根目录执行：

```bash
scripts/dev.sh
```

脚本会依次完成：

1. 检查 Docker、Conda、Go、npm、curl、`jarvis-assistant` 环境和应用端口。
2. 通过 Docker Compose 启动 PostgreSQL 与 Redis，并等待两者健康。
3. 执行 Alembic migration。Gateway 固定监听 `127.0.0.1:8080`；当前未提供远程认证边界，因此不能通过开发环境变量将其公开到局域网。
4. 构建并启动可用的本地 MLX-VLM/BGE Reranker、Python Control Plane、Go Gateway、Python Worker、
   RAG Worker 和 Vue Web。
5. 检查 Control Plane、Gateway、Web 和 Worker heartbeat。
6. 在同一终端聚合输出各服务日志，并监督子进程。

启动成功后访问 `http://127.0.0.1:5173`。按 `Ctrl+C` 会统一关闭四个应用进程，但保留 PostgreSQL、Redis 容器和 PostgreSQL 数据卷，方便下次快速启动。

### 日志文件

应用日志同时输出到终端（彩色）和滚动文件（无颜色）。`scripts/dev.sh` 会为 Gateway、Control Plane、Worker、Web 的服务前缀使用固定颜色，并在真实终端中为后端服务强制保留级别颜色；重定向输出或设置 `NO_COLOR` 时自动关闭。日志文件位于 `.local/logs/`（由 `JARVIS_LOG_DIR` 控制，`scripts/dev.sh` 自动注入）：

| 文件 | 服务 |
|---|---|
| `.local/logs/gateway.log` | Go Gateway |
| `.local/logs/control-plane.log` | Python Control Plane |
| `.local/logs/worker-<id>.log` | Python Agent Worker |
| `.local/logs/rag-worker-<id>.log` | Python RAG Worker |
| `.local/logs/mlx-vlm.log` | 本地 MLX-VLM（启用时） |

本地 BGE Reranker 首次使用前执行 `scripts/rag/setup-bge-reranker.sh`。该脚本使用隔离 venv 安装
PyTorch/Transformers、预下载 `BAAI/bge-reranker-v2-m3` 并显示真实进度；此后 `scripts/dev.sh`
会自动启动它。设置 `JARVIS_LOCAL_RERANKER_ENABLED=false` 可使用确定性降级链路。
本地推理默认在服务就绪前预热，并使用动态批次。可通过
`JARVIS_RAG_RERANKER_BATCH_SIZE`（默认 8）、`JARVIS_RAG_RERANKER_MAX_BATCH_TOKENS`（默认 4096）、
`JARVIS_RAG_RERANKER_MAX_LENGTH`（默认 640）和 `JARVIS_RAG_RERANKER_WARMUP`（默认 true）调整；
降低 token 预算或批次大小可以压低峰值内存，提高最大长度则会增加延迟与内存占用。

日志级别可通过 `LOG_LEVEL` 环境变量控制（DEBUG / INFO / WARN / ERROR），默认 INFO。
单文件最大 20 MiB，最多保留 10 个历史文件。
Gateway 默认不在 INFO 逐条打印成功请求，而是每 5 分钟输出一条运行摘要；可通过
`JARVIS_GATEWAY_SUMMARY_INTERVAL` 调整（例如 `2m`），或设为 `off` 禁用。Control Plane 写请求、
Worker 执行链路和 MLX-VLM 推理/模型加载仍保留 INFO，正常健康检查和只读轮询降为 DEBUG。
详细格式与配置参见 `docs/18-observability-logging-design.md`。

辅助命令：

```bash
scripts/dev.sh doctor      # 检查依赖、生产配置、目录和端口并生成报告
scripts/dev.sh setup       # 创建/更新 Conda 环境并安装全部项目依赖
scripts/dev.sh infra-down  # 停止 PostgreSQL、Redis；不删除数据卷
scripts/dev-runtime-check.sh
```

P7-2 起，`doctor` 会生成 `.local/preflight/<UTC timestamp>/report.json`，并在启动前检查：系统命令、
Docker Compose/daemon、Conda imports、Web/Shared 与 Go 依赖、生产模型/RAG 配置、`.env` 权限、Workspace 允许范围、
Artifact/RAG Asset 目录、可选 MLX-VLM/Reranker 和应用端口。

状态含义：

- `ready`：全部检查通过。
- `degraded`：只有可选能力警告，可以启动。
- `blocked`：存在启动阻断，`doctor/start` 非零退出。

如需把报告写入其他本地目录，可设置 `JARVIS_PREFLIGHT_OUTPUT_DIR`。报告不会保存密钥、密钥变量名、
数据库 URL、模型响应或 Workspace 绝对路径。新建 `.env` 会使用 `0600` 权限；历史文件权限过宽时按
报告建议执行 `chmod 600 apps/agent-worker/.env`。

### 数据库备份、恢复演练与升级

`scripts/dev.sh start` 只核对数据库是否已经到达当前 Alembic head，不再隐式修改 schema。首次初始化或
代码包含新 migration 时，在应用进程停止、PostgreSQL 容器运行的条件下执行：

```bash
scripts/data-lifecycle.py status             # 只读检查 code/database head
scripts/data-lifecycle.py backup             # 在线一致性备份和 catalog 校验
scripts/data-lifecycle.py restore-drill      # 停服后隔离恢复与全量表精确对账
scripts/data-lifecycle.py upgrade --confirm  # 备份、恢复验证、升级、revision 对账
```

`restore-drill` 和 `upgrade` 在 8100/8080/5173 任一端口仍使用时都会阻断。升级不能省略 `--confirm`，也
不能跳过升级前备份和隔离恢复。产物写入 `.local/data-lifecycle/<UTC timestamp>/`，备份和报告为 `0600`；
请把整个时间戳目录作为一次操作证据保存，不要提交到 Git。

升级失败时不要手工修改 `alembic_version`、覆盖源数据库或执行 `docker compose down -v`。保留本轮报告和
备份，先用 `restore-drill` 复核；需要恢复真实源库时必须另行确认目标、停机窗口和可回滚备份，当前入口
只允许自动恢复到受限命名的隔离临时库。

### 运行诊断与脱敏支持包

```bash
scripts/runtime-support.py check   # 生成结构化本地诊断
scripts/runtime-support.py bundle  # 同时生成可传递的 tar.gz 支持包
```

输出位于 `.local/support-bundles/<UTC timestamp>/`。`bundle` 只含 environment、health、log summary、
operations summary、report 和 manifest 六个 JSON；不会包含原始日志、日志文件名、AuditLog、数据库备份、
Artifact、RAG 文档、环境文件、任务正文或模型内容。压缩包和 JSON 权限为 `0600`。

报告为 `degraded` 时先看 `report.json` 的检查 ID：

- `gateway.health`：确认服务是否启动。
- `runtime.bus` / `runtime.dead_letters`：到 Runtime Health 查看 Redis 投影，并以 PostgreSQL 为真源核对。
- `runtime.workers`：检查 Agent/RAG Worker 进程和 heartbeat。
- `storage.reconciliation`：在 Runtime Health 查看对账，不要直接修 Redis 或业务表。
- `capacity.filesystem`：按保留策略清理本地产物，先保留可恢复备份。
- `logs.summary`：确认 `.local/logs/` 权限；需要深入分析时只在本机查看原始日志。

即使 Gateway 已停止，`bundle` 仍会生成本地容量、日志和操作证据，并把 Gateway 标为 warning。支持包不会
自动上传或发送；分享前仍应检查 `manifest.json`，确认成员符合当前白名单。

### 最终 RC2 候选记录

候选源码提交后，在同一个干净 revision 依次执行：

1. 停止普通应用进程，运行 `scripts/p2-runtime-fault-drill.py --keep-infra`。
2. 运行 `scripts/data-lifecycle.py restore-drill`。
3. 运行 `scripts/p2-audit-retention-drill.sh`；该演练使用带标签的隔离容器和数据卷并自动清理。
4. 重新启动完整服务，运行带显式本地数据库连接的 `scripts/release-gate.sh rc2`。
5. 运行 `scripts/runtime-support.py bundle`。
6. 将五类报告路径传给 `scripts/rc2-candidate.py`。

候选入口会重新读取当前 HEAD 和 dirty 状态，并验证所有证据 revision 完全一致。输出在
`.local/release-candidates/`，包含 `record.json` 和 `record.sha256`。生成记录不会创建 Git tag、GitHub
Release、上传支持包或分发任何文件；这些动作必须另行明确授权。

若提示 revision mismatch，不要改 JSON 或复制旧结果；应确认工作区干净后重新运行对应演练。历史 P2/P7
报告可以用于回归分析，但不能冒充本次 RC2 候选证据。

自定义配置仍通过环境变量传入，例如：

```bash
JARVIS_WEB_PORT=5174 \
JARVIS_CONDA_ENV=jarvis-assistant \
JARVIS_WORKSPACE_ROOT=/path/to/workspace \
JARVIS_ALLOWED_WORKSPACE_PATHS=/path/to/workspace:/path/to/another-workspace \
scripts/dev.sh
```

## 本地 .env 加载

Worker 启动时自动加载 `apps/agent-worker/.env`（基于 `__file__` 定位，不依赖 cwd）。
`override=False`：外部环境变量优先。

```bash
# 复制模板（不含真实密钥）
cp apps/agent-worker/.env.example apps/agent-worker/.env

# 编辑 .env，填入真实值
# .env 已被 .gitignore 忽略，不会提交
```

- 生产/容器环境应直接注入环境变量，不依赖本地 `.env`。
- `.env` 不存在时不报错，正常使用默认值或外部环境变量。
- WorkerConfig 只保存 `JARVIS_MODEL_API_KEY_ENV` 指定的环境变量**名**，不保存密钥值。

## 模型 Provider 配置

Worker 默认使用 `JARVIS_MODEL_PROVIDER=deepseek`；自定义 OpenAI-compatible
端点使用 `custom_openai_compatible`。本地 `.env` 或外部环境必须提供完整模型配置：

```bash
JARVIS_MODEL_PROVIDER=deepseek
JARVIS_MODEL_ADAPTER=langchain            # direct 仅作显式迁移回退
JARVIS_MODEL_BASE_URL=https://api.deepseek.com
JARVIS_MODEL_NAME=deepseek-v4-flash
JARVIS_MODEL_THINKING_MODE=disabled
JARVIS_MODEL_API_KEY_ENV=MY_API_KEY      # 密钥所在的环境变量名
MY_API_KEY=sk-...                        # 实际密钥值
JARVIS_MODEL_TIMEOUT_SECONDS=120
JARVIS_MODEL_MAX_RETRIES=1
JARVIS_MODEL_MAX_TOKENS=4096
JARVIS_MODEL_CONTEXT_WINDOW_TOKENS=131072
JARVIS_AGENT_MAX_ITERATIONS=14
JARVIS_AGENT_MAX_RUN_SECONDS=900
```

**注意**：`JARVIS_MODEL_API_KEY_ENV` 保存的是环境变量**名**而非密钥值。实际密钥通过 `os.environ[key_env]` 读取。

自动化测试使用 `httpx.MockTransport`，不会访问真实模型；本 runbook 的网页冒烟验收会调用已配置的真实模型 API。

## Storage 持久化（PostgreSQL + Python Control Plane）

> **架构已切换（2026-07-14）**：Go 不再直接访问 SQLite。持久化通过 Python Control Plane → PostgreSQL 完成。
> 旧 SQLite 代码已删除（`apps/gateway/internal/storage/`）。

推荐直接使用一键启动脚本，它会注入本地默认 PostgreSQL DSN、执行迁移并按依赖顺序启动服务：

```bash
scripts/dev.sh
```

默认连接串为 `postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis`，可通过 `JARVIS_DATABASE_URL` 覆盖。持久化位于 Docker named volume `postgres_data`，重启应用后数据保持完整。

## 前置条件

本地需要具备：

- Docker Desktop（PostgreSQL + Redis）。
- Go toolchain。
- Node.js + npm。
- Conda，以及名为 `jarvis-assistant` 的本地开发环境；缺失时 `scripts/dev.sh setup` 自动创建 Python 3.12 环境。
- uv（CI 固定使用 0.12.5）；`pyproject.toml` 声明依赖范围，`uv.lock` 锁定完整依赖图，依赖同步到现有 Conda 环境。

推荐安装方式：

```bash
scripts/dev.sh setup
```

如需单独更新 Python 依赖：

```bash
agent_python_prefix="$(conda run -n jarvis-assistant python -c 'import sys; print(sys.prefix)')"
UV_PROJECT_ENVIRONMENT="$agent_python_prefix" uv lock --project apps/agent-worker --check
UV_PROJECT_ENVIRONMENT="$agent_python_prefix" \
  uv sync --project apps/agent-worker --frozen --extra dev --inexact
```

`scripts/dev.sh` 未显式配置时会将项目根目录同时作为默认工作区和唯一允许根目录。macOS/Linux 下多个允许根目录使用冒号分隔；值会同时传给 Control Plane、Gateway 和 Worker，三端不得各自使用不同范围。

## 手动启动顺序（仅用于排障）

正常开发使用 `scripts/dev.sh`。只有需要单独观察某个服务时，才按下列顺序拆分启动：

```bash
# Terminal 1: PostgreSQL + Redis
docker compose up -d postgres redis

# Terminal 2: migration + Python Control Plane
cd apps/agent-worker
export JARVIS_DATABASE_URL=postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis
export JARVIS_WORKSPACE_ROOT=/path/to/Jarvis-Assistant
export JARVIS_ALLOWED_WORKSPACE_PATHS=/path/to/Jarvis-Assistant
conda run --no-capture-output -n jarvis-assistant python -m alembic upgrade head
conda run --no-capture-output -n jarvis-assistant \
  python -m uvicorn jarvis_worker.control_plane.app:app --host 127.0.0.1 --port 8100

# Terminal 3: Go Gateway
cd apps/gateway
JARVIS_RUNTIME_BUS=redis \
JARVIS_REDIS_ADDR=127.0.0.1:6379 \
JARVIS_CONTROL_PLANE_URL=http://127.0.0.1:8100 \
JARVIS_WORKSPACE_ROOT=/path/to/Jarvis-Assistant \
JARVIS_ALLOWED_WORKSPACE_PATHS=/path/to/Jarvis-Assistant \
go run ./cmd/gateway

# Terminal 4: Python Worker
cd apps/agent-worker
JARVIS_DATABASE_URL=postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis \
JARVIS_REDIS_ADDR=127.0.0.1:6379 \
JARVIS_WORKER_ID=worker-01 \
JARVIS_WORKSPACE_ROOT=/path/to/Jarvis-Assistant \
JARVIS_ALLOWED_WORKSPACE_PATHS=/path/to/Jarvis-Assistant \
conda run --no-capture-output -n jarvis-assistant python -m jarvis_worker.main

# Terminal 5: Vue Web
cd apps/web
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
```

Header 工作区选择器只展示 Gateway 返回的 `allowed_workspace_paths`，创建 Task 时会发送选中 `workspace_path`。Control Plane 在写入 PostgreSQL 前规范化并校验；越界返回 `WORKSPACE_ACCESS_DENIED`，不存在或非目录返回 `WORKSPACE_NOT_FOUND`。Worker 只接收已经持久化的规范化路径，并由 AgentRunner 覆盖模型可能提供的 `workspace_root`。

## 冒烟验收

仓库提供脚本：

```bash
scripts/dev-runtime-check.sh
```

默认检查：

- `GET /api/health`。
- `GET /api/runtime/workers` 至少存在一个未 stale worker。
- `POST /api/tasks` 可以创建 task/run。
- `GET /api/runs/:id/events` 的 SSE 输出包含 `task.created`。
- SSE 输出包含 worker 产生的 `agent.run.completed`。

可配置项：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `JARVIS_GATEWAY_URL` | `http://127.0.0.1:8080/api` | Gateway API base URL |
| `JARVIS_GATEWAY_HOST` | `127.0.0.1` | Gateway 监听 IP；只允许 `127.0.0.1` 或 `::1`，禁止公开监听 |
| `JARVIS_CHECK_TIMEOUT_SECONDS` | `15` | SSE 读取窗口 |
| `JARVIS_CHECK_REQUIRE_WORKER` | `1` | 是否要求至少一个 active worker |
| `JARVIS_CHECK_REQUIRE_COMPLETION` | `1` | 是否要求 `agent.run.completed` |

示例：

```bash
JARVIS_CHECK_TIMEOUT_SECONDS=30 scripts/dev-runtime-check.sh
```

如果只想隔离验证 Gateway in-memory 测试 adapter，不要求 worker 和 completed event：

```bash
JARVIS_CHECK_REQUIRE_WORKER=0 \
JARVIS_CHECK_REQUIRE_COMPLETION=0 \
scripts/dev-runtime-check.sh
```

## 手动 API 验收

健康检查：

```bash
curl -s http://127.0.0.1:8080/api/health
```

worker 状态：

```bash
curl -s http://127.0.0.1:8080/api/runtime/workers
```

Runtime Health 汇总：

```bash
curl -s http://127.0.0.1:8080/api/runtime/health
```

该接口只返回 Worker 汇总、consumer group lag/pending/consumer/最老 pending、DLQ 数量和累计可靠性指标，不读取消息 payload。`degraded` 表示需要关注运行面，不会覆盖 PostgreSQL 中的 Task/Run 状态。

读取 Run Queue DLQ 脱敏诊断记录：

```bash
curl -s 'http://127.0.0.1:8080/api/runtime/dead-letters?source=run_queue&limit=20'
```

可选 `error_code`、`task_id`、`run_id` 精确筛选，并使用响应的 `next_cursor` 作为 `before` 加载下一页。该接口只返回有界白名单字段，不返回原始 payload，也不执行删除或重放。

Runtime 页面“检查处置”先执行只读资格核对。只有 `RUN_QUEUE_RETRY_EXHAUSTED` 且 PostgreSQL Task/Run/Workspace 仍完全匹配时，才允许创建 L3 单次确认请求。批准会创建新的 Run，不会重放 Redis payload、恢复旧 Run 或删除 DLQ；拒绝也写审计。若显示 `DLQ_*_STATE_CHANGED`、`DLQ_WORKSPACE_UNAVAILABLE` 或 `DLQ_SOURCE_NOT_RETRYABLE`，应先查看 PostgreSQL 任务历史与 Audit 页面，不要绕过限制手工 XADD。

创建任务：

```bash
curl -s \
  -H "Content-Type: application/json" \
  -d '{"user_goal":"验证 Redis worker runtime"}' \
  http://127.0.0.1:8080/api/tasks
```

读取 SSE：

```bash
curl -N http://127.0.0.1:8080/api/runs/<run_id>/events
```

成功链路的关键事件顺序应至少包含：

```text
task.created
agent.run.started
model.call.started
model.call.completed
tool.call.started (需要工具时)
tool.call.finished (需要工具时)
agent.run.completed
```

## Web 手动验收

打开 `http://127.0.0.1:5173` 后：

- Header worker 状态应显示在线 worker。
- Header 和 Sidebar 应显示 worker heartbeat 上报的真实模型名，不出现硬编码供应商。
- Command 输入任务后，主线程应出现 inline run block。
- Timeline 应持续出现 worker 事件。
- 运行结束后 composer 应恢复可输入状态。

如果 Web 没有看到 worker 状态，先用 `/api/runtime/workers` 判断是 Gateway 没消费 heartbeat，还是前端展示问题。

## 常见失败判断

### Gateway redis 模式启动失败

常见原因：

- Redis 未启动。
- `JARVIS_REDIS_ADDR` 指向错误地址。
- Redis 密码或 DB 配置错误。

先运行：

```bash
redis-cli -h 127.0.0.1 -p 6379 ping
```

### worker 状态为空

判断顺序：

```bash
curl -s http://127.0.0.1:8080/api/runtime/workers
```

- 返回空数组：worker 未启动，或 heartbeat pump 未读到心跳。
- `is_stale=true`：worker 曾经上报，但超过 stale timeout。
- worker 日志报 Redis 错误：优先修 Redis 地址或 consumer group。

### 只有 task.created，没有 worker 事件

这通常表示 Gateway 已创建 task/run 并入队，但 worker 没有消费或没有写回 runtime event。

检查：

- worker 是否运行。
- worker 是否连接同一个 Redis 地址。
- worker 日志是否出现 `收到 run job`。
- Gateway 日志是否出现 event pump 读取错误。

### Run Queue pending / retry / DLQ

Worker 默认每 5 秒检查一次 Run Queue PEL，每次至多优先接管一条后再读取新消息；单条消息按 65 / 130 / 260 秒
退避接管，最多交付 3 次。可通过以下环境变量调整，但生产环境不应把 stale 阈值设得
早于 Run lease：

```bash
JARVIS_RUN_QUEUE_RECLAIM_IDLE_MS=65000
JARVIS_RUN_QUEUE_RECLAIM_INTERVAL_MS=5000
JARVIS_RUN_QUEUE_MAX_DELIVERIES=3
```

诊断时先查看 Worker 状态接口中的 `runtime_bus` 累计指标，再查看 PEL 和 DLQ：

```bash
curl -s http://127.0.0.1:8080/api/runtime/workers
redis-cli XPENDING jarvis:stream:run-queue jarvis:group:worker-pool
redis-cli XREVRANGE jarvis:stream:run-dead-letter + - COUNT 20
```

- `retry_deferred` 增长且 PEL 有消息：通常是 PostgreSQL/claim 暂时失败，等待退避重试。
- `reclaimed` 增长：Worker 正在接管崩溃实例遗留的消息；应同时检查 orphan Run reconciliation。
- `dead_lettered` 增长：查看 PostgreSQL `run.queue.dead_letter` 审计和 DLQ `error_code`。
- `malformed` 增长：生产者写入了非法 outer/schema/type/payload，应修复消息契约，不要手工重放原 payload。

DLQ 不是业务真源，也没有自动重放；为避免复制潜在敏感输入，它只保存 payload 的
SHA-256 与字节数。人工处置前必须先核对 PostgreSQL 中 Task/Run、RuntimeEvent 和
AuditLog，不得根据 DLQ 记录绕过正常的恢复与权限链路。

### Worker command / RuntimeEvent pending 与 DLQ

command 默认每秒扫描 PEL，按 5 / 10 / 20 / 40 / 80 秒退避接管。Gateway EventPump 每秒扫描 runtime-event PEL，按 5 / 10 / 20 秒退避，最多投影 3 次：

```bash
JARVIS_COMMAND_RECLAIM_IDLE_MS=5000
JARVIS_COMMAND_RECLAIM_INTERVAL_MS=1000
JARVIS_GATEWAY_ID=gateway-local-01
```

诊断命令：

```bash
redis-cli XPENDING jarvis:stream:worker-command jarvis:group:worker-pool
redis-cli XREVRANGE jarvis:stream:worker-command-dead-letter + - COUNT 20
redis-cli XPENDING jarvis:stream:runtime-event jarvis:group:gateway-events
redis-cli XREVRANGE jarvis:stream:runtime-event-dead-letter + - COUNT 20
```

- command PEL 长期存在时，先核对 PostgreSQL Run、PermissionRequest、worker lease 和 active run；合法 command 不得仅按 delivery count 手工删除。
- `WORKER_COMMAND_*` DLQ 表示生产者 outer/payload 契约错误，应修复 Outbox/transport owner 后重新走业务 API，不复制或直接重放原 payload。
- `RUNTIME_EVENT_PROJECTION_RETRY_EXHAUSTED` 表示 Gateway 临时实时投影未建立；权威事件仍在 PostgreSQL，先验证刷新恢复，再处理 Gateway Seed 时序。
- 两类 DLQ 都只保存 payload 指纹与大小。任何人工恢复仍必须经过正常 API、权限、Storage、EventBus 和 AuditLog 链路。

### SSE 一直不结束

这是预期行为。`/api/runs/:id/events` 是长连接，脚本使用 `curl --max-time` 截取一段输出做冒烟检查。

## 可选：Apple Silicon 本地 PaddleOCR-VL

该能力不会由 `scripts/dev.sh setup` 自动安装或下载模型，重型依赖保存在 Git 忽略的
`.local/rag-runtimes/` 中。安装完成后，MLX-VLM 已纳入 `scripts/dev.sh start` 的统一生命周期：
默认 `JARVIS_LOCAL_VLM_ENABLED=auto`，检测到 MLX-VLM 可执行文件时启动，否则跳过；也可以设置
为 `true` 强制要求运行环境存在，或设置为 `false` 禁用。统一入口负责端口检查、健康检查、日志、
异常监督以及 Ctrl+C 回收。

本地运行时仍分为两个边界：

1. Worker 所在 Python 环境安装 PaddlePaddle 3.2.1+ 与 `paddleocr[doc-parser]`，用于完整 Pipeline
   的布局检测、区域裁剪、阅读顺序和结果合并。
2. 独立虚拟环境安装 `mlx-vlm>=0.3.11`，由统一入口启动仅监听 localhost 的 VLM 服务。

当前开发入口仍使用 Conda `jarvis-assistant` 启动 RAG Worker。若 PaddleOCR 客户端安装在项目隔离
目录，`dev.sh` 会通过 `JARVIS_RAG_PADDLEOCR_SITE_PACKAGES` 把其 site-packages 追加到该 Conda
进程，不会把 MLX-VLM 合并进 Worker，也不会覆盖 Conda 中已经存在的核心依赖。

```bash
scripts/dev.sh start
```

Jarvis 默认连接 `http://127.0.0.1:8111/`，模型名固定为
`PaddlePaddle/PaddleOCR-VL-1.6`，`vl_rec_max_concurrency=1`。Provider 会拒绝 HTTPS、非 loopback
host、URL 凭据、query/fragment 或并发大于 1 的配置。MLX 服务只执行 VLM 元素识别，不能直接替代
完整 PaddleOCR-VL Pipeline。

数字 PDF 中已经由 PyMuPDF 定位的语义图片或不完整表格，会优先裁剪对应区域再交给 Pipeline；扫描页、
区域过多或覆盖过大的页面仍使用整页解析。视觉结果默认缓存在
`.local/rag-cache/structure/`，也可通过 `JARVIS_RAG_STRUCTURE_CACHE_ROOT` 指定其他本地目录。
重新执行相同 PDF 会跳过已经缓存的页/区域；缓存是可重建加速数据，清空后只会触发重新推理，不会改变
PostgreSQL 中的文档、作业、审计或向量真源。

若只诊断模型服务，可独立执行 `scripts/rag/start-mlx-vlm.sh`；它不是日常启动入口。受控样本生成器
位于 `apps/agent-worker/tests/fixtures/create_multimodal_fixture.py`，完整 Jarvis 链路验收程序位于
`apps/agent-worker/tests/integration/jarvis_preprocessing_smoke.py`，具体命令见同目录 README。

2026-07-27 已在 M2 Pro、16 GB 统一内存设备完成真实验收：模型本体约 1.8 GB，布局模型约
126 MB；冷态单图 Pipeline 初始化约 12.7 秒、推理约 57.2 秒，模型热态下完整
“PyMuPDF → PaddleOCR-VL → 多模态分片”单页约 13.5–14.6 秒，客户端峰值 RSS 约 1.2 GB。
受控 PDF 的表格、公式和折线图均进入对应模态节点，Paddle 返回的 HTML 表格会在 Provider
边界规范化为 Markdown 后再交给分片器。

后续设备验收仍至少记录：

- Mac 芯片与统一内存容量。
- 首次/热启动模型加载时间。
- 数字 PDF 与扫描 PDF 每页耗时。
- 峰值统一内存和 swap。
- 取消、服务未启动、模型返回异常时 ingestion job 的恢复结果。
- 表格、公式、图表、双栏阅读顺序和原图证据是否可以追溯。

## Storage 边界

> **2026-07-14 更新**：SQLite 已删除。持久化由 Python Control Plane → PostgreSQL 负责。
> Go Gateway 通过 `JARVIS_CONTROL_PLANE_URL` 配置 Control Plane 地址；Redis 模式缺失该配置时拒绝启动，只有显式 `inmemory` 测试模式允许无 Control Plane。
> Redis 只承载 run queue、worker command、runtime event 和 heartbeat，始终不是业务真源。

## MVP RC1 门禁

开发环境启动并稳定后，先执行基础 Runtime smoke：

```bash
scripts/release-gate.sh runtime
```

准备 RC1 时使用 `docs/20-mvp-rc1-release-gate.md` 的 evidence 模板记录八条真实用户旅程，并运行：

```bash
scripts/release-gate.sh rc1 /absolute/path/to/rc1-evidence.json
```

`redis_state_loss_recovery` 只能在专用验收实例执行。不要对正在承载个人任务的 Redis 执行 `FLUSHALL`，
也不要删除 PostgreSQL volume。完整门禁日志位于 `.local/release-gate/`，不进入 Git。

## RC2 工程门禁

日常提交与 pull request 使用确定性代码门：

```bash
scripts/release-gate.sh ci
```

完整 RC2 工程候选要求服务已启动、工作区干净，并显式指定当前 PostgreSQL：

```bash
JARVIS_DATABASE_URL='postgresql+asyncpg://...' scripts/release-gate.sh rc2
```

结果目录包含人读摘要、逐步骤 TSV、机器可读 `report.json` 和步骤日志。`rc2` 不会替代 P2 隔离故障注入
或备份恢复演练；这些涉及服务重启和临时数据库的操作只在专用候选验收环境执行。
