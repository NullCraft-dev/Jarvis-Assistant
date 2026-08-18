# Python Backend Architecture

## 文档目的

本文档说明 Python Agent Worker（`apps/agent-worker/src/jarvis_worker/`）的当前目录
结构、职责边界与依赖方向。源码采用标准 Python src layout，业务 import 从
`jarvis_worker` 开始。

## 目录结构

```text
jarvis_worker/
├── agent/
│   ├── core/                   # AgentRunner、LangGraph、状态、action 与 effect guard
│   ├── context/                # ContextManager、预算、上下文组装
│   ├── memory/                 # Memory service、Repository 端口与 PostgreSQL adapter
│   ├── rag/                    # RAG 共享领域、Repository/PostgreSQL adapter 与处理流水线
│   │   ├── ingestion/         # Artifact 校验、可恢复应用服务、Asset Store 与 PDF 解析
│   │   ├── preprocessing/     # 统一中间结构、页面路由、Native/VL 融合与 Provider
│   │   ├── chunking/          # 结构化、确定性文本分片
│   │   ├── embedding/         # OpenAI Provider 与 Embedding application service
│   │   ├── indexing/          # VectorIndex 边界与 PostgreSQL/pgvector adapter
│   │   ├── retrieval/         # 可插拔 Rewrite/Retrieve/Rerank/Assemble Pipeline
│   │   ├── answer/            # RagAnswer 契约与可信 CitationValidator
│   │   ├── worker/            # 独立 RAG Worker 配置、装配、循环与进程入口
│   │   └── ocr/               # OCR Provider adapters
│   ├── models/                 # ModelProvider、Provider registry、DeepSeek 等 adapter
│   ├── prompts/                # PromptBuilder
│   ├── skills/                 # 可选领域方法 Skill 加载与受控脚本 Tool executor
│   ├── tool_gateway/           # Gateway、Registry、contracts、module catalog
│   ├── permissions/            # PermissionManager 策略
│   ├── tools/                  # 只放具体工具及其 manifest/binding
│   │   ├── builtin.py
│   │   └── workspace/
│   │       ├── module.py
│   │       ├── path_policy.py
│   │       ├── list_files.py
│   │       ├── get_file_info.py
│   │       ├── search_files.py
│   │       ├── read_file.py
│   │       ├── create_file.py
│   │       ├── create_directory.py
│   │       ├── move_path.py
│   │       └── delete_path.py
│   └── artifacts/              # Artifact service、文件存储与 PostgreSQL adapter
├── runtime/
│   ├── runs/
│   ├── tasks/
│   ├── conversations/
│   ├── workspaces/
│   ├── permissions/            # 权限请求持久化、决定与恢复流程
│   ├── tool_calls/             # ToolCall 持久化
│   ├── audit/
│   ├── worker.py
│   ├── run_executor.py
│   ├── events.py
│   └── service.py
├── runtime_bus/                # Redis queue、command、event 与 heartbeat
├── database/                   # 连接、ORM、UnitOfWork、Outbox/Inbox
├── control_plane/              # FastAPI 短事务 Internal API
├── shared/                     # config、contracts、domain、errors、observability
├── bootstrap/                  # DI 容器、ModelProvider 和 ToolRegistry 组装
├── migrations/                 # Alembic migrations
└── main.py
```

## 核心模块职责

| Package | 职责 |
|---------|------|
| `agent/core/` | 执行模型驱动的 Agent loop，不直接访问 Redis、数据库或本地系统 |
| `agent/context/` | 在模型调用边界生成有预算、可观测的 ContextPackage |
| `agent/memory/` | 长期记忆业务、端口和数据库 adapter 的功能聚合 |
| `agent/rag/` | RAG 文档/作业/分块/多模态元素共享领域及数据库 adapter；具体处理按 ingestion/preprocessing/chunking/embedding/indexing/retrieval/ocr 分包，`worker/` 拥有独立进程生命周期 |
| `agent/models/` | 屏蔽供应商差异，提供统一 ModelProvider |
| `agent/skills/` | 校验/选择可选领域方法 Skill；不拥有产品工作流、证据或能力生命周期，并把显式启用的确定性脚本装配成受控 system Tool |
| `agent/tool_gateway/` | 工具统一注册、参数校验、权限入口与结果契约，不拥有具体工具 |
| `agent/permissions/` | 工具执行前的风险判断与授权策略 |
| `agent/tools/` | 只拥有具体工具、Manifest、executor binding 和同领域安全策略 |
| `runtime/` | 组织 Worker、Run、Task、Conversation、Workspace、Permission 和 ToolCall 生命周期 |
| `runtime_bus/` | Redis Runtime Bus adapter；Redis 不是业务真源 |
| `database/` | 公共连接、ORM、事务与可靠 Outbox/Inbox，不拥有业务决策 |
| `control_plane/` | 开发/调试及 Go Gateway 使用的短事务接口，不执行长任务 |
| `shared/` | 跨模块共享的配置、消息契约、领域对象、错误和可观察性 |
| `bootstrap/` | 唯一依赖组装入口 |

Agent Worker 与 RAG Worker 是两个进程边界。前者消费 Redis Agent Run 并执行 Harness；后者只通过
`RagIngestionService` / `RagEmbeddingService` 领取 PostgreSQL RAG Job。RAG Worker 的依赖装配留在
`agent/rag/worker/bootstrap.py`，不会进入通用 Agent `bootstrap/container.py`。它单独发布 heartbeat，
但不携带 Agent model status；Web 可以同时观察两个 Worker。生产进程由 Conda 环境启动，PaddleOCR
客户端的重型依赖通过显式 `JARVIS_RAG_PADDLEOCR_SITE_PACKAGES` 追加，MLX-VLM 保持独立 localhost
服务。`RagIngestionCommandService` 只负责入队，可安全装配进 Agent Tool；处理型 Service 只属于
RAG Worker。

在线查询属于 Agent Worker 的低延迟读取能力，但检索算法仍由 `agent/rag/retrieval/` 拥有。
`RagRetrievalPipeline` 固定 QueryRewriter/CandidateRetriever/Reranker/ContextAssembler 端口，默认
实现只是第一组装配，不是硬编码算法。PostgreSQL adapter 通过 `HnswSearchConfig` 在当前事务内设置
HNSW 搜索宽度和迭代上限，SQL 只负责距离排序，Python Retriever 负责有限候选的稳定 tie-break；
`agent/rag/reranking/` 拥有 HardFilter、Feature、Provider、语义排名融合、MMR、Policy 和组合阶段，
具体模型 adapter 不得进入 retrieval Pipeline。BGE 模型运行在独立 localhost sidecar，Agent Worker
只依赖 `RerankerProvider`。语义阶段只能重排已有候选，确定性 Policy 始终负责最终约束。这些数据库/模型细节不得进入
Tool 契约。`agent/tools/rag/` 只保存 L0 manifest 与同步 ToolGateway
adapter，不允许直接访问 pgvector。`agent/rag/answer/` 从当前 Run 的可信 RAG observation 归一化
引用；AgentRunner 只消费通用 `FinalAnswerValidator` 端口。

## Tool 与 ToolGateway 边界

```text
AgentRunner
  -> ToolGateway
  -> PermissionManager
  -> ToolRegistry
  -> agent/tools/<domain>/<tool>
  -> ToolResult
  -> Runtime persistence / Audit / RuntimeEvent
```

- `agent/tools/` 不允许放 Gateway、PermissionManager、Repository 或数据库连接。
- `agent/tool_gateway/` 不允许放 Workspace 等具体 executor。
- `agent/permissions/` 只负责执行前策略；`runtime/permissions/` 负责请求保存、用户决定和恢复。
- `runtime/tool_calls/` 保存 ToolCall 记录；它不是工具实现的一部分。
- `CapabilityModule`/`ToolBinding` 是 ToolGateway 的装配契约，具体 module 与 manifest
  跟随 `agent/tools/<domain>/`。

## 依赖方向

```text
main
  -> bootstrap
  -> runtime
  -> agent/core
  -> agent/tool_gateway
       -> agent/permissions
       -> agent/tools
       -> agent/skills/SkillScriptExecutor

runtime -> runtime_bus
runtime -> Repository / UnitOfWork
agent/rag -> Repository / UnitOfWork（只由后续 application service 组织）
database -> shared/domain
control_plane -> runtime services
```

禁止方向：

```text
agent/tools -> runtime
agent/tools -> database client
agent/core -> Redis
agent/core -> PostgreSQL
database -> AgentRunner
runtime_bus -> runtime business decisions
control_plane -> LangGraph node
```

## 物理目录与架构层的关系

Storage Access Layer 仍然存在，但不再强制使用通用 `storage/` 目录。Repository
优先跟随所属功能，例如：

```text
agent/memory/repository.py
agent/memory/postgres_repository.py
runtime/workspaces/postgres_repository.py
runtime/tool_calls/postgres_repository.py
```

公共 PostgreSQL 连接、ORM 和 UnitOfWork 位于 `database/`。功能聚合不能被解释为允许
业务代码绕过 Repository、事务、权限、审计或 RuntimeEvent。

## P6 Agent Runtime 模块收口方向

当前 `agent/core/graph.py` 已是 LangGraph 图装配 owner，`graph_state.py` 是进程内状态 owner，
`graph_nodes.py` 负责路由。P6-3 第一切片已新增 `core/phases/runtime.py` 与
`core/phases/observation.py`：前者提供有界图更新、checkpoint 与事件原语，后者成为
`observe_result` 的业务 owner。第二切片新增 `core/phases/model_call.py`，拥有上下文预算、Skill、
安全 streaming 和模型错误/重试语义，只依赖项目 `ModelProvider`。两个 phase 都没有工具执行或权限
能力。第三切片新增 `core/phases/action_validation.py`，拥有动作/effect/最终答案校验和可信 ToolRequest
构造，只能经 ToolGateway `assess` 获取评估，不能执行 effect。第四切片新增
`core/phases/tool_execution.py`，成为唯一通过 ToolGateway 执行 effect 的图阶段，并拥有权限事件与
defer checkpoint 语义。第五切片新增 `core/phases/intent_extraction.py`，拥有可信 Intent 上下文、提取、
纠错与能力检查，但不拥有 retry/call_model/end 路由。最终切片新增 `core/phases/lifecycle.py`，拥有
运行初始化、取消/暂停检查和最大迭代终态，但不拥有条件路由。`AgentRunner` 已缩减至 639 行；
`graph.py` 与 `graph_nodes.py` 继续独占 7 组条件边和 7 个 route 函数。P6 按
`model call / action validation / tool execution / observation / terminal` 拆出窄 phase services；
`AgentRunner` 最终只协调输入、依赖和图执行，不再同时拥有每个阶段的全部实现。

P6-2 已在 `agent/models/` 增加 `langchain_provider.py`、`langchain_factory.py` 和共享
`provider_config.py`：前两者实现现有 `ModelProvider` 与供应商装配，后者让 direct/LangChain 共用同一
配置和密钥读取边界。它们不得进入 `runtime/`、`tool_gateway/`、Persistence 或 Web DTO。LangGraph
内部 state 仍只属于 `agent/core/`，不得成为 Redis envelope 或数据库 schema。迁移顺序与验收门禁见
`docs/26-p6-agent-runtime-framework-consolidation.md`。

P6-4 决定不启用 LangGraph native interrupt 或 checkpointer：跨 Worker 权限恢复继续通过 PostgreSQL
PermissionRequest/Run checkpoint 与 Redis command 完成。`graph.py` 只编译拓扑，不拥有第二套持久化
状态；任何后续重议必须先证明可与现有业务 checkpoint 原子对账且不会重复 effect。
