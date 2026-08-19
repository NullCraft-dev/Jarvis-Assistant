# 技术栈文档

## 技术栈目标

技术栈应优先服务以下目标：

- 优先通过 Web 端快速验证完整 Agent 控制台。
- 前端、Go 运行时服务层、Python Agent Worker 可以分开开发、分开测试、分层替换。
- Go 层为前端提供稳定 API 契约、校验、事件扇出，并负责多进程 Agent 运行的调度治理。
- Redis 作为运行时通信层，承载任务队列、命令、事件、worker 心跳和短期协调状态。
- Python worker pool 承载完整 Agent Runtime、LangChain / LangGraph、工具、权限、存储和审计。
- FastAPI 只作为 Python 侧可选的 dev/debug/control plane，不作为长任务或多 Agent 执行热路径。
- 后续能复用稳定 UI 和 Runtime 能力封装成 MacBook 上的桌面 App。
- 能安全调用本地电脑能力。
- 能持久化任务、日志、设置、权限、工具调用和记忆。
- 能逐步支持云端 LLM、本地 LLM、MCP、多 Agent 和后台长任务。

## MVP 推荐技术栈

```text
Web App:
  Vue 3 + TypeScript + Vite

UI:
  Naive UI as primary component library
  UnoCSS or Tailwind CSS for layout utilities
  Iconify or lucide-vue-next for icons

Frontend State:
  Pinia for local UI state
  TanStack Query for Vue or VueUse + typed API client for server state

Go Gateway / Runtime Orchestrator:
  Go 1.25.0
  go-redis v9.21.0 作为 Redis Streams adapter
  net/http + Chi, Echo, Gin, or Fiber
  DTO validation
  ApiResult / AppError normalization
  Worker manager / scheduler
  go-redis v9.21.0 for Redis Streams producer / consumer
  SSE / WebSocket event fan-out
  Permission command routing
  Runtime diagnostics API

Redis Runtime Bus:
  Redis Streams / consumer groups preferred
  Go side: go-redis v9.21.0（GoRedisStreamClient adapter）
  Python Worker side: redis-py 或等价库（后续，必须遵守同一消息契约）
  run queue
  worker command stream
  runtime event stream
  worker heartbeat / status
  pending permission and cancellation signal

Python Agent Worker Pool:
  Python
  httpx for OpenAI-compatible LLM API calls (no vendor SDK)
  LangChain for model, prompt, tool wrapper, retriever, parser (later)
  LangGraph for stateful agent loop, graph orchestration, human-in-the-loop
  ToolGateway / PermissionManager / Storage / AuditLog

Optional Python Control Plane:
  FastAPI may be used for dev/debug/control endpoints only
  Do not use FastAPI request lifecycle for long-running AgentRun execution

Contracts:
  OpenAPI / JSON Schema as Web transport contract
  Runtime command / event envelopes for Go <-> Redis <-> Python workers
  TypeScript types for Web
  Go structs for Gateway / Orchestrator
  Python Pydantic models for workers

Storage:
  Storage Layer abstraction first
  Local relational backend candidate later
  Redis is not business truth

Desktop later:
  Reuse stable Vue Web UI and backend contracts through Electron or another desktop shell
```

## 为什么 MVP 优先 Web

当前阶段最难的是 Agent Runtime、工具权限、任务状态、可观察性和产品交互闭环，不是桌面壳本身。Web 优先可以更快验证 Command Center、任务时间线、权限接管、设置页、运行诊断和 artifact 预览，同时保持后续桌面端复用空间。

Web 优先的收益：

- Vue + Vite 开发和调试速度快。
- 更容易迭代信息架构、状态展示和权限交互。
- 可以先把 DTO、RuntimeEvent、Go Runtime Orchestrator、Redis Runtime Bus、Python Worker Runtime、ToolGateway、PermissionManager 和 MCP Adapter 的边界做稳定。
- 后续桌面端可以复用稳定后的 Vue UI，并只新增桌面 shell、preload / IPC adapter 和 macOS 能力。

Web 优先的边界：

- 不是普通远端聊天网页。
- 不允许用前端 mock 替代真实 Runtime 契约。
- 不提前把核心业务锁死到 Electron IPC。
- 本地文件、Shell、系统能力仍必须通过 Python worker 内的 ToolGateway 和 PermissionManager。

## 前端技术栈

### Vue 3 + TypeScript + Vite

Vue 3 负责构建 Web Agent 控制台。TypeScript 用于约束 DTO、API client、RuntimeEvent 映射和复杂页面状态。Vite 用于快速开发、构建和后续桌面端复用。

前端主线：

```text
Vue Web
-> typed API client
-> Go Gateway / Runtime Orchestrator
-> Runtime event stream
```

### 组件库选择

首选 `Naive UI`。

原因：

- 适合控制台、设置页、权限弹窗、数据表、抽屉、Tabs、Timeline、Badge、Notification 等高密度工具型界面。
- 暗色主题和主题定制能力较好，适合 Jarvis / Agent Console 气质。
- 视觉比传统中后台组件库更现代，不需要大量重写。

可选备选：

- `Arco Design Vue`：适合更企业中后台风格。
- `Element Plus`：生态成熟，但视觉更传统，除非后续需要更强表单/表格生态，否则不是首选。

辅助工具：

- `UnoCSS` 或 `Tailwind CSS`：做布局、间距、响应式和少量视觉定制。
- `Iconify` 或 `lucide-vue-next`：统一图标。
- `Pinia`：保存本地 UI 状态。
- `TanStack Query for Vue` 或 `VueUse + typed API client`：管理 server state、缓存、请求状态和错误。

## Go Gateway / Runtime Orchestrator

Go 层不只是 BFF。它是前端契约守门人，也是多进程 Agent 运行的 Runtime Orchestrator。

Go 层负责：

- API Gateway / BFF。
- request DTO 校验。
- response shape 统一。
- `ApiResult` / `AppError` 包装。
- 前端会话、鉴权、trace id、限流、超时和日志。
- 创建任务入口、run 入队和运行命令路由。
- Redis run queue / command stream / event stream 的 producer 和 consumer。
- worker manager：worker 心跳、状态、并发、背压、取消、超时、重试和恢复触发。
- SSE / WebSocket event fan-out。
- Worker 状态、健康检查和运行诊断 API。
- 屏蔽 Python worker 内部结构。

Go 层不负责：

- 不执行 LangGraph Agent loop。
- 不直接调用 LLM。
- 不直接执行 native / MCP / system 工具。
- 不实现 Memory / Context 核心逻辑。
- 不替代 Python PermissionManager 的核心决策。
- 不把 Redis 状态当成 Task / Run / Step 的最终真相。

推荐路径：

```text
Vue Web
-> Go Gateway API
-> Go Runtime Orchestrator
-> Redis Runtime Bus
-> Python Agent Worker Pool
```

## Redis Runtime Bus

Redis 是运行时通信层，不是业务数据库。

适合放在 Redis 的内容：

- `run.queue`：待执行 AgentRun。
- `worker.command`：pause / resume / cancel / retry / permission decision。
- `runtime.events`：task、run、step、model、tool、permission、artifact 事件。
- `worker.heartbeat`：worker 存活、并发、负载和能力标签。
- `pending.permission`：等待用户确认的短期运行信号。
- backpressure / rate limit / short-lived lock。

不适合只放在 Redis 的内容：

- Task、AgentRun、ExecutionStep、ToolCall、PermissionRequest、PermissionGrant、AuditLog 的最终状态。
- 用户设置、模型配置、MCP server 配置和长期记忆。
- artifact 文件和大文本内容。

Redis 选型建议：

- MVP 多进程阶段优先使用 Redis Streams + consumer groups。
- run queue 和 runtime event stream 都使用显式 envelope，包含 `id`、`trace_id`、`task_id`、`run_id`、`type`、`timestamp` 和 `payload`。
- worker 需要幂等处理消息，Go 需要处理 pending / retry / dead letter。
- 生产与本地完整链路默认使用 Redis；in-memory bus 只作为显式测试替身实现同一接口。
- `JARVIS_REDIS_ADDR/PASSWORD/DB` 是跨语言连接契约，Go Gateway、Python Control Plane、Agent Worker 和
  RAG Worker 必须共同消费；密码不得出现在配置 repr、日志、DTO 或诊断报告中。

## Python Agent Worker Pool

Python Agent Worker Pool 是完整 Agent Runtime 的执行位置。

负责：

- Agent Runtime。
- LangChain / LangGraph。
- ModelProvider。
- ContextManager。
- MemoryManager。
- ToolGateway。
- PermissionManager。
- MCP Adapter。
- Storage / AuditLog。
- Task / AgentRun / ExecutionStep / ToolCall / Permission 的真实状态写入。
- 工具执行。
- LLM 调用。
- 长任务恢复。
- 将 RuntimeEvent 写入 Redis Runtime Bus。

Python worker 不负责：

- 直接服务 Web UI。
- 解析 Go Gateway 的 Web DTO 细节。
- 管理全局 worker 进程调度。
- 不通过 FastAPI 长连接承载 AgentRun 热路径。

## FastAPI 定位

如果引入 FastAPI，它只适合：

- Python worker 的健康检查和本地调试。
- 开发期查看 runtime 内部状态。
- 读取 worker 健康状态和运行诊断信息。
- 少量 control plane API。

FastAPI 不适合：

- 作为每个 AgentRun 的长请求生命周期。
- 承载多 Agent 的并行执行调度。
- 代替 Redis command / event bus。
- 成为前端直接依赖的业务 API 真源。

## LangChain 与 LangGraph 分工

LangChain / LangGraph 是 Python Agent Worker 内的能力和编排组件，不是整个项目的最终真源。

### 当前依赖基线（2026-08-19）

- 生产热路径已经使用 LangGraph `StateGraph`，项目直接声明 `langgraph>=1.0.10,<2.0`。
- 项目显式精确锁定 LangChain Core 1.5.1、LangChain OpenAI 1.4.1、LangChain DeepSeek 1.1.0；
  当前锁文件使用 LangGraph 1.2.11、LangGraph Checkpoint 4.2.0 和 LangGraph SDK 0.4.2。Checkpoint、SDK
  与 Cryptography 同时声明安全版本下限，使 CI 的普通 `pip install` 与 `uv.lock` 都不会重新解析到已知
  易受攻击版本。项目不安装完整 `langchain` 聚合包。
- DeepSeek 使用官方 `ChatDeepSeek`，自定义 OpenAI-compatible 使用 `ChatOpenAI` 的项目窄适配；两者
  都实现项目 `ModelProvider`，框架消息和异常不进入 Runtime 或 Web 契约。
- `JARVIS_MODEL_ADAPTER=langchain` 是默认生产路径；`direct` 保留原同步 `httpx` Provider 作为显式
  回退。运行失败不自动切换，避免重放模型调用或重复用户可见流式文本。

### LangChain

LangChain 负责“能力组件”：

- ModelProvider：统一模型调用封装。
- Provider 标识表达真实供应商/后端（当前为 `deepseek` 或
  `custom_openai_compatible`），协议实现独立复用；DeepSeek 与其他供应商的扩展参数
  不进入通用 OpenAI-compatible 适配器。
- 结构化输出能力按 Provider 声明和实现：DeepSeek 独立启用官方
  `response_format=json_object`；自定义 OpenAI-compatible 默认只依赖项目 Prompt 与
  ActionParser，不假设目标模型支持同名参数。
- Prompt / ChatPromptTemplate：提示词和消息格式化。
- Tool wrapper：把内部工具适配成模型可见工具描述，但真实执行仍必须走 ToolGateway。
- Retriever / Embedding / Document loader：用于记忆、项目知识库和 RAG。
- Output parser：把模型输出解析为结构化计划、工具请求或最终结果。

LangChain 不负责：

- 权限最终决策。
- ToolGateway 边界。
- AuditLog 规范。
- 用户可见 DTO 和 RuntimeEvent 契约。
- Storage schema 真源。

### LangGraph

LangGraph 负责“运行编排”：

- AgentRunner loop。
- AgentRun 状态图。
- human-in-the-loop / permission wait。
- pause / resume / retry / blocked。
- long-running task recovery。
- multi-agent task graph。
- node-level event 映射。

典型图结构：

```text
build_context
-> call_model
-> decide_next_action
-> check_permission
-> execute_tool
-> observe_result
-> verify
-> finish
```

LangGraph node 可以调用 LangChain；LangChain tool 必须通过我们的 ToolGateway；LangGraph state 必须同步成我们的 Task / AgentRun / ExecutionStep / RuntimeEvent。

## Harness / Loop Engineering 定位

本项目属于 Harness Engineering / Loop Engineering。

```text
LangChain = 模型、prompt、工具、retriever、parser 等能力组件
LangGraph = Agent loop / graph 编排组件
Go Runtime Orchestrator = 并发、进程、队列、命令、事件和前端契约治理
Redis Runtime Bus = 跨进程运行时通信
Python Agent Worker = Agent Runtime 大脑
Vue Web = 用户观察、接管和控制台
Storage / Audit / Permission / ToolGateway = Harness 的安全和状态系统
```

Harness 的真源不是 LangChain、LangGraph 或 Redis，而是项目自己的：

- Task / AgentRun / ExecutionStep / ToolCall 状态。
- ToolGateway。
- PermissionManager。
- AuditLog。
- Storage。
- RuntimeEvent。
- AppError。
- API contract。

## 存储策略

MVP 不直接把业务逻辑绑定到具体数据库。项目应先定义 Storage Layer 和 Store interfaces，再选择一个适合本地开发和后续本地部署的持久化 backend。

Storage Layer 负责：

- task
- agent_run
- execution_step
- tool_call
- conversation
- setting
- permission_grant
- audit_log

核心接口：

```text
TaskStore
RunStore
StepStore
ToolCallStore
PermissionStore
AuditStore
MemoryStore
SettingsStore
McpStore
ArtifactStore
RagDocumentStore
RagIngestionJobStore
RagChunkStore
RagElementStore
RagAssetStore
RagChunkElementLinkStore
```

数据库候选：

- 本地轻量关系型数据库：适合 MVP、部署简单、离线优先。
- PostgreSQL：适合后续更强 JSON 查询、并发、远程/私有化部署和 pgvector。
- RAG 关系型元数据：由 PostgreSQL 保存文档、入库作业、分块正文与来源定位。
- 向量索引：通过 `VectorIndex` 端口隔离，当前 adapter 使用 pgvector 0.8.2、1536 维 cosine HNSW；
  Embedding 默认使用 OpenAI `text-embedding-3-small`，密钥只从本地环境变量读取，
  领域层不绑定具体实现。
- PDF 原生解析：使用 PyMuPDF 1.28.x，从受控 Artifact bytes 提取文字块、页面坐标、表格结构和
  图片裁剪；不接受任意文件路径，也不把解析能力暴露给 Renderer。
- 本地文档视觉解析：Apple Silicon 使用完整 PaddleOCR-VL Pipeline，客户端执行 PP-DocLayout 系列
  布局检测、区域裁剪和阅读顺序恢复，VLM 阶段通过 localhost MLX-VLM 服务运行
  `PaddlePaddle/PaddleOCR-VL-1.6`。MLX 服务不能被当作完整文档解析 API 直接调用。
- 资源治理：PaddleOCR 客户端 `vl_rec_max_concurrency=1`，Jarvis Provider 也用单并发队列；PyMuPDF
  原生解析可独立执行，扫描页进入整页视觉解析，数字页的语义图片/不完整表格优先只解析有界区域；区域
  过多或覆盖过大时回退整页。版本化本地视觉结果缓存支持重启后复用已完成区域，但不承载业务状态。
- RAG 执行进程：`python -m jarvis_worker.agent.rag.worker` 是独立于 Agent Worker 的常驻进程；
  第一版直接通过 PostgreSQL 的 claim/lease/retry 领取 RAG Job，顺序轮转 ingestion 与 embedding。
  Redis run queue 仍只承载 AgentRun，避免混淆两套任务语义。
- RAG 在线检索：查询 Embedding 使用 OpenAI `text-embedding-3-small`，语义召回使用 pgvector
  cosine HNSW；查询期 `ef_search`、迭代扫描、最大访问节点数和扫描内存通过 retrieval adapter 在
  单次事务内配置，不能修改连接池全局状态。数据库只提供距离顺序，有限候选的确定性 tie-break 由
  Python Retriever 完成。关键词路由使用 PostgreSQL 有界词项/短语覆盖，双路结果以归一化 RRF 融合。
  `RagRetrievalService` 返回结构化 `RagContextPackage`。当前不增加 Elasticsearch 或专用 BM25
  服务。Cross-Encoder 默认选用独立 localhost sidecar 中的 `BAAI/bge-reranker-v2-m3`，模型不加载进
  Agent Worker；sidecar 在健康就绪前完成一次推理预热，并按实际 token 长度、批次条数和单批 token
  预算动态组批，避免短文本被长文本无效 padding。默认最大序列长度 640、最多 8 条/批、4096 token/批；
  没有 Provider 时显式降级到确定性 Feature/MMR/Policy 链路，不得伪装成语义重排成功。
- 云端 fallback：百度智能云高精度 OCR adapter 保留但默认关闭；启用时属于外部数据传输。
  `VisualDescriptionProvider` 暂只有契约，派生描述不能替代原图证据。
- Artifact store：大文本、截图、diff、文件产物可以放本地文件系统，由数据库保存 metadata。

当前优先级是把 Gateway 契约、Redis Runtime Bus envelope、RuntimeEvent、Storage Interface、ToolGateway、PermissionManager 和 MCP Adapter 的边界做稳定，而不是把业务逻辑塞进某个框架。

## 通信策略

当前主线：

```text
Vue Web
-> Go Gateway / Runtime Orchestrator
-> Redis Runtime Bus
-> Python Agent Worker Pool
```

普通请求：

```text
Vue typed API client
-> Go REST API
-> Go handler validates DTO
-> Go orchestrator reads/writes Storage through approved service boundary or enqueues runtime command
```

运行命令：

```text
Go Runtime Orchestrator
-> Redis run queue / worker command stream
-> Python Agent Worker
```

运行事件：

```text
Python Agent Worker
-> Redis runtime event stream
-> Go event consumer / fan-out
-> Vue RuntimeEvent stream
```

权限决策：

```text
Python Worker emits permission.required
-> Redis runtime event stream
-> Go event fan-out
-> Vue Permission Dialog
-> Vue resolvePermission
-> Go Gateway
-> Redis worker command stream
-> Python Worker resumes or denies
```

事件类型示例：

```text
task.created
agent.run.started
agent.step.started
model.delta
tool.call.started
tool.call.finished
permission.required
permission.resolved
agent.run.completed
agent.run.failed
```

后续桌面端封装时，可以增加 Electron preload / IPC adapter，但它只是 transport 的一种实现，不应该改变共享 DTO、RuntimeEvent、ToolGateway 或 PermissionManager 语义。
