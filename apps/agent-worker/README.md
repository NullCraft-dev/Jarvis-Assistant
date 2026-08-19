# Jarvis Python Agent / RAG Workers

Python Agent Worker Runtime — 消费 Redis run queue，执行真实模型驱动的 Agent loop，产出 RuntimeEventEnvelope 写入 Redis runtime event stream。

同一 Python 包还提供独立 RAG Worker。它不执行 Agent loop，而是从 PostgreSQL 领取可恢复的 RAG
入库作业，单并发执行多模态预处理、分块、OpenAI Embedding 与 pgvector 入库。

## 链路

```
Redis run queue (jarvis:stream:run-queue)
  → Python agent-worker consume RunJobMessage
  → AgentRunner (OpenAiCompatibleModelProvider)
  → ToolGateway → 产出 RuntimeEventEnvelope
  → 写入 Redis runtime event stream
  → Go EventPump → InMemoryRuntimeBus
  → SSE → Web UI Timeline
```

## 源码结构

`src/` 采用标准 Python src layout；业务 import 从 `jarvis_worker` 开始，不包含
`src`。Worker 内部按功能聚合，而不是把同一功能拆散到通用 application/storage
目录：

```text
jarvis_worker/
├── agent/          # core、context、memory、models、tool_gateway、permissions、tools、artifacts
├── runtime/        # Worker 执行以及 run/task/conversation/workspace/permission 流程
├── runtime_bus/    # Redis run queue、command、event 与 heartbeat
├── database/       # 公共连接、ORM、事务与 outbox 基础设施
├── control_plane/  # 可选开发/调试 API，不进入长任务热路径
├── shared/         # config、contracts、domain、errors、observability
├── bootstrap/      # 依赖组装
├── migrations/     # Alembic migrations
└── main.py
```

各功能的 service 和 PostgreSQL repository 放回所属功能目录。例如长期记忆位于
`agent/memory/`，工作区位于 `runtime/workspaces/`。`database/` 不拥有业务决策，
只提供公共数据库能力。

## 快速启动

```bash
conda activate jarvis-assistant
cd apps/agent-worker
UV_PROJECT_ENVIRONMENT="$CONDA_PREFIX" uv lock --check
UV_PROJECT_ENVIRONMENT="$CONDA_PREFIX" uv sync --frozen --extra dev --inexact

# 配置本地模型与密钥（必需）
cp .env.example .env
# 编辑 .env 填入真实值（.env.example 不含真实密钥，.env 已被 .gitignore 忽略）

# 启动真实 DeepSeek 模式（可由 .env 提供以下变量）
# .env 只补充缺失变量，外部已注入的优先。生产/容器直接注入环境变量。
JARVIS_MODEL_ADAPTER=langchain \
JARVIS_MODEL_PROVIDER=deepseek \
JARVIS_MODEL_BASE_URL=https://api.deepseek.com \
JARVIS_MODEL_NAME=deepseek-v4-flash \
JARVIS_MODEL_THINKING_MODE=disabled \
JARVIS_MODEL_API_KEY_ENV=MY_API_KEY \
MY_API_KEY=sk-... \
JARVIS_WORKSPACE_ROOT=/path/to/your/project \
python -m jarvis_worker.main

# 另一个进程：独立 RAG Worker（通常由项目根目录 scripts/dev.sh 统一启动）
python -m jarvis_worker.agent.rag.worker
```

## .env 加载规则

- 启动时自动加载 `apps/agent-worker/.env`（基于 `__file__` 定位，不依赖 cwd）。
- `override=False`：外部环境变量优先于 `.env`。
- `.env` 不存在时不报错；存在但加载失败 → 启动失败。
- 真实密钥只填 `.env`，`.env.example` 不含真实密钥。WorkerConfig 不保存密钥值。

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `JARVIS_REDIS_ADDR` | `127.0.0.1:6379` | Redis 地址 |
| `JARVIS_WORKER_ID` | `worker-01` | Worker 标识 |
| `JARVIS_WORKER_GROUP` | `jarvis:group:worker-pool` | Consumer group |
| `JARVIS_WORKER_CONSUMER` | 同 `JARVIS_WORKER_ID` | Consumer 名称 |
| `JARVIS_RUN_QUEUE_RECLAIM_IDLE_MS` | `65000` | stale RunJob 首次可接管时间，最小 65 秒 |
| `JARVIS_RUN_QUEUE_RECLAIM_INTERVAL_MS` | `5000` | Worker 扫描 PEL 的间隔 |
| `JARVIS_RUN_QUEUE_MAX_DELIVERIES` | `3` | RunJob 最大交付次数，超限进入 DLQ |
| `JARVIS_COMMAND_RECLAIM_IDLE_MS` | `5000` | worker command 首次可接管时间，最低 1 秒 |
| `JARVIS_COMMAND_RECLAIM_INTERVAL_MS` | `1000` | Worker 扫描 command PEL 的间隔 |
| `JARVIS_WORKSPACE_ROOT` | 空 | 默认 workspace 根目录 |
| `JARVIS_MODEL_ADAPTER` | `langchain` | `langchain` 为主路径；`direct` 保留为迁移回退 |
| `JARVIS_MODEL_PROVIDER` | `deepseek` | `deepseek` 或 `custom_openai_compatible`；旧 `openai_compatible` 仅作迁移别名 |
| `JARVIS_MODEL_BASE_URL` | DeepSeek 官方端点 | API base URL；自定义兼容 Provider 必填 |
| `JARVIS_MODEL_NAME` | 空 | 模型名称（如 deepseek-v4-flash） |
| `JARVIS_MODEL_API_KEY_ENV` | 空 | 存放 API key 的环境变量**名**（不保存密钥值） |
| `JARVIS_MODEL_TIMEOUT_SECONDS` | `120` | 请求超时秒数 |
| `JARVIS_MODEL_MAX_RETRIES` | `1` | 最大重试次数（0-2） |
| `JARVIS_MODEL_MAX_TOKENS` | `4096` | max_tokens 参数 |
| `JARVIS_MODEL_CONTEXT_WINDOW_TOKENS` | `131072` | 模型上下文窗口；作为 ContextManager 输入预算真源 |
| `JARVIS_AGENT_MAX_ITERATIONS` | `14` | 单个 Run 的工具调用预算；严格限制为 1–20，避免普通任务出现无界工具循环 |
| `JARVIS_MODEL_THINKING_MODE` | `""` | `""` \| `"disabled"`（DeepSeek V4 需 disabled） |

## 测试

```bash
conda activate jarvis-assistant
cd apps/agent-worker
python eval/runners/fetch_corpus.py \
  --case nist-ai-rmf-1-0 \
  --case nasa-systems-engineering-handbook-rev2 \
  --case world-bank-data-driven-development-2018
UV_PROJECT_ENVIRONMENT="$CONDA_PREFIX" uv lock --check
UV_PROJECT_ENVIRONMENT="$CONDA_PREFIX" uv sync --frozen --extra dev --inexact
pytest -v
```

固定评测 PDF 只保存在本地 cache，下载时按版本化 manifest 校验大小和 SHA-256，不提交到 Git。

静态检查和格式验证统一通过仓库脚本执行：

```bash
../../scripts/check-python-quality.sh
```

脚本对全库执行高风险基线，并对当前新增/修改的 Python 文件追加严格 Ruff 检查；不要求启动服务。

自动化测试使用 `fakeredis` / `httpx.MockTransport` / 直接注入的模型测试替身，零网络访问。测试替身不会通过生产配置或 Web API 启用。

## 当前范围

- LangGraph 编排的 AgentRunner + OpenAiCompatibleModelProvider
- ToolGateway、权限恢复与 workspace native tools
- Redis Worker heartbeat / cancel，以及 PostgreSQL 持久化与审计

确定性模型与执行测试替身位于 `tests/testing_doubles.py`；生产 `src/jarvis_worker`
包不包含、也不能通过配置启用 Mock 或 Dev 场景。

## 明确不做

- MCP、Multi-agent 自由讨论与完整长期记忆
