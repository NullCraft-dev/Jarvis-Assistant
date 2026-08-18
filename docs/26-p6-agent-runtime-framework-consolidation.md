# P6 Agent Runtime 框架收口

## 目标

P6 在 P5 工程化与产品化闭环之后，优先收口 Python Agent Worker 内的 LangChain 能力适配和
LangGraph 编排职责。它不是一次框架替换，也不是检索算法优化或 Multi-Agent 扩展。

目标是让框架真正服务于既有 Harness：减少重复的模型协议代码，缩小单体 `AgentRunner`，让图节点、
恢复点和 RuntimeEvent 的对应关系更清楚，同时保持 ToolGateway、PermissionManager、PostgreSQL、
AuditLog 和 RuntimeEvent 的唯一真源地位。

## P6-1 当前实现审计（2026-08-02）

### 已经存在的能力

| 审计面 | 当前事实 | 判断 |
| --- | --- | --- |
| LangGraph | `AgentRunner.run()` 和 checkpoint 恢复均调用真实 `StateGraph.invoke()` | 已接入生产热路径，不是占位实现 |
| 图结构 | `initialize_run → extract_intent → call_model → validate_action → execute_tool → observe_result`，另有最大迭代终态 | 控制流真实，但节点主要委托回 `AgentRunner` |
| 工具安全 | `execute_tool` 仍经 `ToolGateway → PermissionManager → ToolExecutor` | 必须保持 |
| 恢复真源 | PostgreSQL `agent_runs.checkpoint_json` 与 `permission_requests.checkpoint_json`，当前 checkpoint version 为 4 | 不启用第二套业务 checkpointer |
| 模型抽象 | 项目自有 `ModelProvider`、`ModelMessage`、PromptBuilder、ActionParser | 边界可复用 |
| 生产模型调用 | `OpenAiCompatibleModelProvider` 通过同步 `httpx.Client` 直接调用 chat completions | 尚未经过 LangChain adapter |
| LangChain | 项目没有显式安装完整 `langchain`，`langchain-core` 仅由 LangGraph 间接带入 | 不能把“设计上计划使用”写成“已经完成接入” |
| Runner 体量 | `agent/core/runner.py` 约 2181 行；`graph.py` 60 行，`graph_nodes.py` 159 行 | 业务阶段 owner 仍过度集中 |

当前 `pyproject.toml` 只声明 `langgraph>=0.2,<1.0`。`uv.lock` 锁定 LangGraph 0.6.11、
LangGraph Checkpoint 3.0.1 和 LangChain Core 1.5.1；本地生产 Conda 环境实际为 LangGraph 0.6.11、
LangGraph Checkpoint 3.0.1 和 LangChain Core 1.4.9，且未安装完整 `langchain`。`pip check` 没有发现
破损依赖，但锁文件与运行环境的 Core 小版本漂移必须在引入 adapter 前消除。

### 当前结论

1. 项目需要的是 LangChain adapter，而不是再“接一次 LangGraph”。
2. LangGraph 下一步应接管更清晰的阶段状态转换，不应接管权限、工具执行、持久化或前端契约。
3. 不能直接换成预制 ReAct agent 或 `ToolNode`；这会绕过既有 action validation、effect guard、
   ToolGateway、权限 checkpoint、审计和恢复语义。
4. 不把 LangGraph 原生 checkpointer 引入为平行业务真源。若后续使用 interrupt 等能力，必须通过
   明确 adapter 映射到现有 PostgreSQL checkpoint，并证明不会重复模型调用或工具 effect。
5. P6 不修改 RAG 算法。P5 的门禁与问题台账继续只输出诊断，具体召回、重排和 Context 优化后置。

## 目标边界

```text
LangGraph node
-> project phase service
-> ModelProvider / ToolGateway / checkpoint service
-> Storage + AuditLog + RuntimeEvent
```

LangChain 只实现项目端口：

```text
project ModelMessage / Prompt input
-> LangChain chat model adapter / structured output
-> trusted project AgentAction or AppError
```

框架类型不得穿过 `ModelProvider`、ToolGateway、RuntimeEvent 或 Web DTO。供应商 SDK、LangChain message、
Runnable result、LangGraph internal state 和原始异常都只能留在 Python Worker adapter 内。

## 分阶段计划

### P6-2：LangChain 模型适配层

状态：已完成。

- `pyproject.toml` 与 `uv.lock` 精确锁定 LangChain Core 1.5.1、LangChain OpenAI 1.4.1 和 LangChain
  DeepSeek 1.1.0；项目 `.venv` 与实际 Conda `jarvis-assistant` 环境版本一致，完整 `langchain` 聚合包
  仍未安装。
- `LangChainModelProvider` 实现现有 `ModelProvider`；`ModelMessage` 只在 adapter 边界转换，工具观察仍
  作为带 Runtime 信任标签的不可信 human data，不启用供应商原生 tool call。
- DeepSeek 使用官方 `ChatDeepSeek`，保留 JSON Output、`thinking=disabled`、项目重试预算和安全纠错；
  自定义兼容端点使用 `ChatOpenAI` 的窄子类，保留既有 `max_tokens` 请求契约。
- `JARVIS_MODEL_ADAPTER=langchain|direct`：`langchain` 是默认生产路径，`direct` 只作为显式迁移回退；
  失败时不自动切到 direct，避免重复模型调用或重复流式输出。
- LangChain 响应仍必须通过 finish reason、纯文本、响应长度、原生 tool call 拒绝、ActionParser 和
  effect guard；第三方异常只按类型/HTTP 状态映射，不复制原始异常、响应或密钥。

### P6-3：LangGraph 节点 owner 与异步边界

状态：已完成。

- 将模型调用、动作校验、工具执行、观察投影和终态构造拆成窄的 phase services，逐步缩小
  `AgentRunner`；图节点拥有 typed state transition 和路由，phase service 拥有本阶段业务语义。
- 每次只迁移一个节点，并保持 RuntimeEvent 顺序、Step 身份、checkpoint 恢复点和错误码完全对账。
- `ainvoke`、异步 Provider 或并发节点必须先验证 Worker 线程、Redis command、取消和数据库事务边界；
  不以“异步化”为目标制造第二套执行模型。

前两切片结果：

- `ObservationPhase` 已拥有成功、可恢复失败与不可恢复失败的 ToolResult 投影语义，图节点直接绑定其
  `run()`；该 service 没有 ToolGateway、PermissionManager 或 executor 能力。
- `ModelCallPhase` 已拥有上下文预算、Skill 解析、安全 streaming、模型异常映射与 Runtime 结构化纠错；
  它只依赖项目 `ModelProvider`，不导入 LangChain 实现类型、ToolGateway 或 Permission owner。
- `ActionValidationPhase` 已拥有 AgentAction/effect/final answer 校验和可信 `ToolRequest` 构造；它只调用
  ToolGateway `assess`，结构门禁禁止 `execute`、Permission owner 直连或 LangChain 类型泄漏。
- `ToolExecutionPhase` 已成为图内唯一 effect owner；保留权限 required/resolved/expired、defer、取消、
  暂停和 tool-in-flight checkpoint，只经 ToolGateway `execute`，不导入具体 executor。
- `IntentExtractionPhase` 已拥有可信目录、规则/LLM Intent、纠错和能力检查；LangGraph
  `route_after_intent` 继续拥有 retry/call_model/end 分支，phase 不调用后续节点。
- `PhaseRuntime` 统一图游标、checkpoint、envelope 与 AppError 原语；`AgentGraphUpdate` 为节点提供
  进程内 typed transition，但不成为新的持久化状态或 DTO。
- 事件序列、Step 身份、`call_model` checkpoint、流式上限/分片、publish 时机和 ToolResult 公开 shape
  保持不变；`AgentRunner` 从 2181 行缩减至 687 行。
- `RunLifecyclePhase` 已拥有运行初始化、取消/暂停检查和最大迭代失败终态；它不调用其他 phase，
  `AgentGraphNodes` 直接绑定所有业务/lifecycle phase。
- 价值审计确认 `graph.py` 仍是唯一拓扑 owner：7 组条件边和 7 个 route 函数负责 START 恢复、Intent
  重试、动作分流、校验重试、工具观察循环和终态选择；phase services 只拥有业务语义，不自行推进流程。
- 第五切片完整回归为 `1239 passed`；最终生命周期切片定向回归 `185 passed`，项目 `.venv` 与生产
  Conda 完整回归均为 `1241 passed`。`AgentRunner` 最终从 2181 行缩减至 639 行。
- P6-3 保留同步 `StateGraph.invoke()`；异步化、LangGraph interrupt 和 checkpointer 没有因拆分而隐式启用，
  统一进入 P6-4 做恢复真源与 human-in-the-loop 对账。

### P6-4：恢复与 human-in-the-loop 对账

状态：已完成。

- 评估 LangGraph interrupt/resume 是否能适配现有 PermissionApplicationService 和 checkpoint v4。
- 覆盖允许、拒绝、过期、取消、Worker 崩溃、恢复预算耗尽与工具 effect 未知场景。
- PostgreSQL 继续是恢复真源；任何 LangGraph 内部状态必须能由持久化业务状态重建或安全失败关闭。
- 第一切片新增集中 Permission checkpoint builder/validator：构造时校验内部 job/state/tool 身份，Worker
  在 claim 和 effect 前把 request/task/run/step/tool-call/tool-name 与 PostgreSQL PermissionRequest
  逐项对账。同版本损坏以 `PERMISSION_CHECKPOINT_INVALID` fail closed，旧版本继续使用 incompatible
  错误；两者都不得进入 ToolGateway execute。
- 第一切片定向回归 `159 passed`，项目 `.venv` 与生产 Conda 完整回归均为 `1242 passed`。
- 第二切片让所有 `agent.run.failed` 在同一事务内关闭开放 Step/ToolCall；allow resume lease 过期使用
  `PERMISSION_RESUME_EFFECT_UNKNOWN` 不可恢复收口，deny 中断使用独立错误码。已消费命令只在安全
  `call_model` checkpoint 存在时保留恢复资格，任何路径都不重复进入 ToolGateway execute。
- 第二切片定向回归 `42 passed`，项目 `.venv` 与生产 Conda 完整回归均为 `1245 passed`。
- 第三切片让已过期 permission command 与已终态 cancel command 按 PostgreSQL 身份幂等 ack；不重复
  恢复 Runner 或发布终态，身份不匹配仍进入 reclaim/DLQ。`ToolCall.permission_status` 缺少
  `expired` 的 schema/DTO 缺口已记录，迁移前不得伪装成用户 deny。
- 第三切片定向回归 `53 passed`，项目 `.venv` 与生产 Conda 完整回归均为 `1246 passed`。
- 第四切片经用户确认增加 `ToolCall.permission_status=expired`：migration、ORM/fresh schema、Runtime
  投影、共享 DTO、Gateway 与 Web Inspector 同步更新。只有 pending 权限可过期，approved/denied
  不被覆盖；页面明确显示“授权已过期”。
- 第四切片双 Python 环境完整回归均为 `1249 passed`，Web `129 passed` 与生产构建、共享类型检查、
  Gateway 全量 Go 测试均通过；Alembic 唯一 head 为 `024_tool_permission_expired`。

最终恢复矩阵：

| 场景 | PostgreSQL 权威事实 | 收口结果 | 是否重放工具 effect |
| --- | --- | --- | --- |
| allow，尚未 claim | approved request + waiting Run | 校验身份后条件 claim，一名 Worker 执行 | 仅首次 claim 执行 |
| deny | denied request | resolved + failed，工具未执行 | 否 |
| expired / Run 已终态 | expired request 或 terminal Run | 迟到 command 幂等 ack | 否 |
| Worker 在 pre-effect 节点崩溃 | v4 checkpoint 位于 resumable node | 有界重排，最多 3 次 | 未发生的 effect 不重放 |
| Worker 在 `tool_in_flight` 崩溃 | effect 结果未知 | `RUN_RECOVERY_UNSAFE` / permission effect unknown | 禁止 |
| 工具终态已持久化、后续推理中断 | consumed request + `call_model` checkpoint | 可恢复后续模型轮次 | 不重新执行工具 |
| checkpoint 旧版本、损坏或身份不一致 | 权威校验失败 | incompatible/invalid fail closed | 否 |
| 恢复预算耗尽 | recovery_attempts 达上限 | `RUN_RECOVERY_EXHAUSTED` | 否 |

LangGraph native interrupt 在 P6-4 明确保持关闭。当前权限恢复要求原 Worker 可退出并由任意空闲 Worker
接手；没有共享 checkpointer 的 interrupt 无法跨进程恢复，而新增共享 checkpointer 会与 PostgreSQL
checkpoint/PermissionRequest 形成双真源。除非后续能证明单一 adapter 可原子映射业务 checkpoint、
不会重复模型/工具 effect 且故障注入优于当前方案，否则不重新开启该决策。

P6-4 最终恢复矩阵与架构门禁定向回归 `57 passed`，项目 `.venv` 与生产 Conda 完整回归均为
`1251 passed`。

### P6-5：可观察与真实验收

- 将 node、model、tool、retry、pause/resume 映射到既有 Timeline/Inspector，不新增前端猜测状态。
- 用真实任务验证长内容、流式输出、权限接管、SSE 重连、取消和 Worker 重启。
- 对迁移前后做事件序列、终态、工具调用次数、checkpoint 和审计记录差异对账。

首个可观察切片已完成：既有 model/tool 映射保持不变，retry/pause/resume 由统一 presentation
消费后端明确发布的 `retry_from_checkpoint` 与 `resume_node`。四个可恢复节点只映射为产品语义，未知
节点降级为“安全检查点”；Timeline 展示过程，Context Inspector 展示最近一次恢复位置，原始事件名与
内部 node 仍只在技术诊断层出现。本切片不修改 RuntimeEvent 契约、运行状态机或恢复真源。

第一轮真实验收中，故障注入、普通 SSE 重连、Worker/Redis 重启、权限恢复、长输出暂停/恢复和等待
权限取消均通过，但暴露一个事件顺序阻断：跨 pause/resume 的恢复流会在 `agent.run.completed` 之后
补发恢复前产生的 `model.delta`。这不会改变 PostgreSQL 业务终态，却破坏“终态收口 Timeline/正文”的
传输语义；同时公开 RuntimeEvent 未携带数据库 `event_sequence`，现有门禁无法验证真正的严格单调。
Gateway 已在 SSE owner 层修复该问题：durable/ephemeral 事件按 Runtime 时间有序合并，公开
RuntimeEvent 可选投影 PostgreSQL sequence，终态成为不可跨越的业务栅栏。真实暂停/恢复 Run 使用
paused event id 重连后只返回 sequence `8-11` 的恢复事件并以 completed 收口，使用 completed event id
再次重连返回 0 条业务事件。P6-5 至此完成，不改变 PostgreSQL checkpoint、Runtime 状态机或
LangGraph 编排边界。

## 每个切片的门禁

- 同一输入的 Task/Run 终态与可信 RuntimeEvent 序列等价。
- 恢复后不会重复模型调用、工具调用或本地/外部 effect。
- L0 自动执行、L2/L3 授权、拒绝和 L4 强制确认保持原有行为与 AuditLog。
- pause、cancel、retry、max iterations、上下文/token/输出/工具调用上限仍然生效。
- 所有框架异常映射为安全 `AppError`，不向 Renderer 泄漏 prompt、token、密钥或堆栈。
- 通过 AgentRunner、权限、恢复、Worker flow 定向回归和完整 Agent Worker 回归。
- 涉及用户可见状态时，补真实 Gateway + Web 旅程；纯 adapter 切片不伪造 UI 收益。

## 禁止路线

- `LangGraph prebuilt agent / ToolNode -> native tool / MCP / filesystem / shell`
- LangChain tool 绕过 ToolGateway 或 PermissionManager。
- LangGraph checkpointer 与 PostgreSQL 同时成为恢复真源。
- 为使用框架而重写稳定的 Storage、Audit、RuntimeEvent 或 Web DTO 契约。
- Provider 或框架对象跨越项目 `ModelProvider` / RuntimeEvent 边界。
- 在 P6 混入 Multi-Agent、桌面端或无质量证据的 RAG 算法改造。

## P6-1 验收基线

- AgentRunner、pause/resume、permission、run recovery、model message、context 和 worker flow 定向测试：
  `216 passed`。
- 完整 Agent Worker 回归：`1216 passed`；Ruff、文档索引链接与 diff 格式检查通过。
- 完整回归保留 1 条 LangGraph cache serialization pending deprecation 和既有 PyMuPDF/SWIG 上游
  deprecation 告警；它们不是本轮引入，LangGraph 告警应在 P6-2 依赖统一后重新确认。
- 本轮只更新设计与阶段真源，不修改 API、DTO、schema、migration、权限规则或运行时代码。

## P6-2 验收基线

- LangChain adapter、配置、原 direct Provider 和消息契约定向回归通过；Conda 生产环境的
  LangChain/AgentRunner/Intent 定向回归 `252 passed`，依赖检查无破损。
- 项目 `.venv` 与生产 Conda 环境分别完成完整 Agent Worker 回归，均为 `1233 passed`；Ruff、
  `compileall` 与 diff 格式检查通过。
- 默认 `langchain/deepseek` 真实全链路任务正常完成：11 个事件、371 字符流式输出、模型阶段
  3156 ms、整次运行 5218 ms，Web 正文完整且浏览器控制台无 error / warning。
- 真实验收出现 1 次既有类型的 Control Plane run history 瞬时读取失败，但未影响终态、事件或 UI；
  作为原有 Runtime 稳定性债务保留，不视为 P6-2 阻断。
- P6-2 不修改 ToolGateway、Permission、checkpoint、RuntimeEvent、API、DTO 或 schema。
