# 应用日志系统设计

## 文档目的

定义 Jarvis Assistant 本地应用日志系统的格式、配置、脱敏、文件滚动、故障降级和验收规则。明确应用日志与 RuntimeEvent、AuditLog 的职责边界。

## 职责边界

| 系统 | 职责 | 不负责 |
|---|---|---|
| **应用日志**（本文档） | 开发与运行排障、服务内部状态、错误诊断 | 用户可见任务进度、权限决策审计、UI 展示 |
| **RuntimeEvent** | 任务进度、工具调用、权限请求和产物的用户可见状态 | 服务内部错误诊断、文件日志 |
| **AuditLog** | 权限、安全与本地影响操作的持久化审计 | 应用运行状态、开发排障 |

关键原则：
- 应用日志 ≠ RuntimeEvent ≠ AuditLog
- 不要把应用日志写入 RuntimeEvent 或 AuditLog
- 不要让 UI / Renderer 直接读取日志文件
- 日志失败不得阻塞任务执行、Gateway 请求或 Worker 主循环

## 统一输出格式

终端和文件使用同一套字段顺序。终端可着色，文件绝不包含 ANSI 控制字符。

```
2026-07-24 10:24:31.127 | INFO  | agent-worker/worker-01 | MainThread | jarvis_worker.tool_gateway.gateway.execute:150 | trace=tr_91c2 request=req_18 task=task_3 run=run_a82f step=step_1 | 工具执行结束: tool=workspace.read_file ok=True duration_ms=84 result_kind=text
```

字段固定 7 列，以 ` | ` 分隔：

| 列 | 字段 | 说明 |
|---|---|---|
| 1 | 时间 | 本地时间 `YYYY-MM-DD HH:mm:ss.SSS` |
| 2 | 级别 | `DEBUG` / `INFO ` / `WARN ` / `ERROR`（固定宽度 5） |
| 3 | 服务/实例 | `gateway/gateway-01`、`control-plane/control-plane-01`、`agent-worker/worker-01`、`mlx-vlm/mlx-vlm-01` |
| 4 | 执行上下文 | Python：真实 thread name（如 `MainThread`）；Go：`-`（不获取 goroutine ID） |
| 5 | 调用位置 | `module.function:line` 或 `package/function:line` |
| 6 | 关联上下文 | `trace=... request=... task=... run=... step=...`，缺失显示 `-` |
| 7 | 消息 | 单行化（换行符替换为空格），脱敏后输出 |

### 服务名与实例 ID

| 服务 | 服务名 | 默认实例 ID | 日志文件名 |
|---|---|---|---|
| Go Gateway | `gateway` | `gateway-01` | `gateway.log` |
| Python Control Plane | `control-plane` | `control-plane-01` | `control-plane.log` |
| Python Agent Worker | `agent-worker` | `worker-01` | `worker-<id>.log` |
| Python RAG Worker | `rag-worker` | `rag-worker-01` | `rag-worker-<id>.log` |
| MLX-VLM 外部服务 | `mlx-vlm` | `mlx-vlm-01` | `mlx-vlm.log` |

- 实例 ID 优先读取 `JARVIS_INSTANCE_ID`，未配置时使用默认值。
- Python Worker 的实例 ID 优先级为 `JARVIS_INSTANCE_ID` → `JARVIS_WORKER_ID` → `worker-01`。
- Control Plane 不读取 `JARVIS_WORKER_ID`；未设置 `JARVIS_INSTANCE_ID` 时固定使用 `control-plane-01`，避免误标为 Worker。

### 终端颜色

默认仅当 stderr 为 TTY 且 `NO_COLOR` 未设置时启用。通过 `scripts/dev.sh` 启动时，脚本会检测外层真实终端并向被管道汇总的子进程传递 `JARVIS_LOG_COLOR=always`，因此颜色不会因服务输出经过管道而丢失。

| 字段 | 颜色 |
|---|---|
| 时间 | 绿色 |
| DEBUG | 灰色 |
| INFO | 青色 |
| WARN | 黄色 |
| ERROR | 红色 |
| 服务/实例 | Gateway 蓝色；Control Plane 青色；Agent/RAG Worker 与 MLX-VLM 洋红色 |
| 调用位置 | 蓝色 |
| 其他辅助字段 | 灰色 |

`NO_COLOR` 存在时始终禁用颜色。非 TTY 默认禁用；可显式设置 `JARVIS_LOG_COLOR=always` 强制启用，或 `JARVIS_LOG_COLOR=never` 强制关闭。写入文件时始终禁用颜色。

## 文件输出

### 目录

- 环境变量 `JARVIS_LOG_DIR` 指定日志目录。
- `scripts/dev.sh` 启动的所有服务统一写到仓库根目录 `.local/logs/`。
- 本地直接启动时，未设置该变量默认使用 `<项目根目录>/.local/logs/`；实现会从服务工作目录或 Python 源码路径向上定位项目根目录，因此不会因从 `apps/gateway` 或 `apps/agent-worker` 启动而分散日志。

### 文件命名

| 服务 | 文件名 |
|---|---|
| Gateway | `gateway.log` |
| Control Plane | `control-plane.log` |
| Worker | `worker-<worker_id>.log` |
| RAG Worker | `rag-worker-<worker_id>.log` |
| MLX-VLM | `mlx-vlm.log` |

### 滚动策略

- 单文件最大 20 MiB。
- 最多保留 10 个历史文件（`.1` ~ `.10`）。
- 文件编码 UTF-8。
- `.local/logs/` 已加入 `.gitignore`。

### 故障降级

- 日志目录不可写或文件创建失败时：仅 stderr 告警，应用继续运行。
- 日志写入失败：不抛出异常，不阻塞请求处理。
- 绝不因日志故障递归触发更多日志。

## 关联上下文

通过 Logger API 显式传入结构化上下文字段：

| 字段 | 说明 |
|---|---|
| `trace_id` | 端到端操作 ID；创建 Task 时贯穿 Gateway、Control Plane、Outbox、Redis、Worker、Model、Tool 和 RuntimeEvent |
| `request_id` | 单次 HTTP 请求 ID；用于区分同一端到端操作中的入口请求 |
| `task_id` | 任务 ID |
| `run_id` | 运行 ID |
| `step_id` | 步骤 ID |

- Python：通过 `logging.LoggerAdapter` 或 `contextvars` 传递。
- Go：通过 `slog.With("trace_id", ...)` 或 `logging.CtxFields` 传递。
- 不通过解析自然语言消息提取上下文。
- Gateway 校验 `X-Trace-ID` / `X-Request-ID` 的字符集和长度；无效值会生成新 ID，并将二者写入 request context、响应 header 和 Control Plane 内部请求。
- 创建 Task 时，Control Plane 将入口 `trace_id` 显式传给 TaskApplicationService，持久化到 Outbox，并由 RunJob 继续传给 Worker；不能从自然语言日志或进程局部状态反推。
- Worker 收到 RunJob 后绑定 `trace_id/task_id/run_id`，主执行线程及 command poll 子线程继承该上下文，任务收口后必须清理，避免污染下一条 Run。

## 关键链路埋点

INFO 只记录低频生命周期节点：

```text
HTTP request
→ Task/Run transaction
→ Outbox publish
→ RunJob consume/claim
→ Conversation history + Memory load
→ Context prepare
→ Model call
→ Permission decision
→ ToolGateway execute
→ RuntimeEvent persist/publish
→ Run terminal
```

- Model 日志只记录 provider、model、action 类型、耗时和安全统计，不记录完整 prompt/response。
- Tool 日志只记录工具名、风险、参数 key、结果类型、耗时和稳定错误码，不记录完整参数或文件内容。
- RunJob/WorkerCommand 的 Outbox 投递属于 INFO；durable RuntimeEvent 的逐条成功投影、投递和 ACK 属于 DEBUG；失败、重试耗尽和 DLQ 使用 WARN/ERROR。
- Gateway 正常 2xx/3xx HTTP 请求统一使用 DEBUG；4xx 使用 INFO，5xx 使用 ERROR。默认 INFO 不再逐请求刷屏。
- Gateway 每 5 分钟输出一条聚合运行摘要，包含 Worker 在线/忙碌/失联数、Redis stream pending/lag、DLQ 总数及 EventPump 恢复计数；非 healthy 状态使用 WARN。
- Control Plane 成功 GET 使用 DEBUG，写请求使用 INFO，4xx 使用 INFO，5xx 使用 ERROR；Uvicorn 原生 access log 不再重复输出。
- MLX-VLM 无法直接依赖项目 logger，由启动边界的外部日志适配器转换为同一 7 列格式；成功的 `/openapi.json`、`/health`、`/v1/models` GET access log 使用 DEBUG，推理请求、模型加载和错误按原级别保留。

## 脱敏

对常见敏感键和值脱敏：

### 敏感键名（大小写不敏感）

`key`, `api_key`, `apikey`, `token`, `secret`, `password`, `cookie`, `credential`, `passwd`, `pwd`, `access_key`, `secret_key`, `private_key`, `api_secret`

### 敏感值模式

- `Bearer <token>` → `Bearer ***`
- `sk-` 前缀密钥 → `sk-***`
- JWT token（三段 base64）→ `***`

### 脱敏规则

- `key=value` 和 `key: value` 模式中，敏感 key 的 value 替换为 `***`。
- 引号中的敏感值同样脱敏。
- Go `slog` 的非关联属性（如 `error`、`status`、`duration_ms`）以脱敏后的 `key=value` 追加到第 7 列消息，不能静默丢弃。
- 外部文本、异常信息和关联 ID 会单行化、替换竖线并限长，不能伪造新的日志列。
- 不记录 `.env` 内容、API key、Authorization、Cookie、完整 prompt、模型完整原始响应、文件全文。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `JARVIS_LOG_DIR` | `<项目根目录>/.local/logs` | 日志文件目录 |
| `JARVIS_INSTANCE_ID` | 服务默认值 | 实例 ID |
| `LOG_LEVEL` | `INFO` | 日志级别：DEBUG / INFO / WARN / ERROR |
| `JARVIS_LOG_COLOR` | `auto` | 终端颜色模式：`auto` / `always` / `never`；`NO_COLOR` 优先级更高 |
| `NO_COLOR` | 空 | 设置后禁用终端颜色 |
| `JARVIS_GATEWAY_SUMMARY_INTERVAL` | `5m` | Gateway 聚合运行摘要间隔；Go duration 格式，`off` 或 `0` 禁用 |

## 不记录的内容

- 高频心跳（保持 DEBUG 或不记录）
- SSE ping / token delta
- `.env` 内容
- API key 完整值
- Authorization / Cookie header
- 完整 prompt 内容
- 模型完整原始响应
- 文件全文
- 完整工具参数

## ERROR 日志规范

- 包含安全的错误摘要和稳定 error code（存在时）。
- 不把原始异常、敏感响应直接暴露到日志消息。
- 异常类型可记录，异常原文需脱敏处理。

## 验收规则

1. INFO 行符合固定 7 列格式。
2. ERROR 和 WARN 级别正确显示。
3. 缺失关联上下文时显示 `-`（不改变列结构）。
4. 敏感字段被脱敏。
5. 文件中不存在 ANSI escape sequence。
6. 日志目录不可写不会导致核心流程崩溃。
7. 现有 Go tests、Python pytest、Web build 不回归。

## 实现文件

| 服务 | 文件 |
|---|---|
| Python | `apps/agent-worker/src/jarvis_worker/shared/observability/__init__.py`（公共 API） |
| Python | `apps/agent-worker/src/jarvis_worker/shared/observability/logging.py`（完整实现） |
| Control Plane | `apps/agent-worker/src/jarvis_worker/control_plane/main.py`（日志优先的 Uvicorn 标准入口） |
| Go | `apps/gateway/internal/observability/logger.go`（完整实现） |
| Go | `apps/gateway/internal/app/runtime_summary.go`（Gateway 周期运行摘要） |
| 外部服务 | `scripts/external_log_adapter.py`（MLX-VLM stdout 统一格式、脱敏、滚动） |
| MLX-VLM 启动 | `scripts/rag/start-mlx-vlm.sh`（接入外部日志适配器） |
| 启动 | `scripts/dev.sh`（注入 `JARVIS_LOG_DIR`） |
| 忽略 | `.gitignore`（`.local/logs/`） |

## 限制与后续

- 当前阶段不做 Web 日志页面、Redis 日志流、数据库日志表、SSE 日志接口。
- 不修改 RuntimeEvent、AuditLog、Task/Run 状态机或前端代码。
- Gateway、Control Plane、Worker 是独立进程，各自写独立日志文件，绝不并发写同一文件。
- 后续可考虑：日志级别动态调整（SIGHUP）、日志格式切换（JSON 输出）、日志聚合（开发环境）。

## P7-4 脱敏支持包

`scripts/runtime-support.py` 不复制本节定义的原始日志文件。它最多读取每个日志段末尾 5 MiB、最多 50 个
文件，仅输出按服务聚合的 DEBUG/INFO/WARN/ERROR 数量、总字节数和 WARN/ERROR 代码调用位置。实际文件名、
消息正文和关联 ID 均不进入支持包。

支持包用于定位“哪个服务、哪类运行面、哪个代码 owner”异常，不替代 RuntimeEvent、AuditLog 或 PostgreSQL
业务真源，也不执行修复。完整成员白名单和使用方式见 `docs/27-p7-engineering-release-productization.md`。
