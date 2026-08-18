# 开发路线图

## 当前阶段：预发布核心能力稳定化（暂停 RC 候选）

2026-08-12 决策：当前版本不进入预发布发布流程，也不定义为新的 RC 候选。分支
`codex/pre-release-stabilization` 的干净 revision
`51d85d47ceaa1b0ffdfee2681972452f57dd97d2` 固定为本阶段优化前 baseline；P7 已完成的工程化与发布能力
继续保留，但不能用历史门禁结果替代当前 revision 的真实使用验证。

本阶段的目标不是继续增加功能，也不是针对单条评测案例追加 prompt、关键词、路径或答案特例，而是把
真实使用中反复出现的问题归并到唯一责任层，完成 Agent 核心链路的机制级优化。固定顺序如下：

1. **基线与可观测证据审计**：固定代码、配置、模型、fixture 和评测范围；建立从用户输入、Intent、计划、
   ToolGateway、Observation、证据账本、终态校验到最终回答的完整 trace，先确认问题发生在哪一层。
2. **Harness Engineering**：优先审计状态持久化、ToolGateway/Permission/Audit 不变量、预算、超时、重试、
   取消、恢复、幂等和终态一致性，避免上层用 prompt 补偿执行骨架缺陷。
3. **Loop Engineering**：优化计划、探索、读取、补证、策略切换和停止条件；解决重复搜索、无进展循环、
   过早收口、错误工具选择以及工具预算与任务复杂度不匹配。
4. **RAG Engineering**：在生产 Pipeline 上系统审计查询理解、召回、分块、去重、重排、多文档覆盖、Context
   组装、引用绑定与无充分证据拒答；不得为某份文件或某条预设答案固定检索路径。
5. **Context / Evidence Engineering**：统一 Workspace、RAG、对话和 Knowledge 的证据 owner、来源优先级、
   上下文压缩与跨 Run 恢复，防止旧证据污染、来源串线、信息丢失和证据槽假阳性。
6. **同 revision 发布验证**：完成机制级自动回归后，才在一个新的干净 revision 上依次执行自动门禁、完整
   36 条基础 P0、39 条基础 P1 连续使用场景、核心链路重复轮和恢复/安全验证。测试期间任何代码、Prompt、
   配置、模型或 fixture 变化都会产生新候选，旧 revision 的通过结果不能拼接成发布结论。

重新进入预发布候选阶段至少要求：P0 阻断清零；P1 全部有可审计结果且无未处置阻断；高风险核心链路在
同一 revision 连续两轮无偶发回归；权限绕过、虚假证据、重复副作用、恢复不一致和敏感信息泄漏为零；
自动化、真实 Web 结果和缺陷台账能够互相追溯。满足这些条件前，阶段结论保持 `NO-GO`。

本阶段仍不进入桌面封装、Multi-Agent、插件市场或 Codex/Coding Agent 扩展；基础 P0/P1 继续以 Web 优先、
本地优先的 Personal Agent 真实使用场景为边界。

以下 P0–P7 内容保留为已完成能力和历史路线记录。

MVP RC1 已于 2026-07-30 在 revision `32ccf35092c006428ba85faa1d4400b9006ecbb7` 通过同一 revision
的 G0、G1、G2 和最终 `rc1` 门禁。随后优先完成 Phase 8 Audit / Recovery /
Persistence Hardening，没有进入 Multi-Agent 或桌面封装。

P2 以可重复故障注入、长任务与 SSE 稳定性、migration/备份/恢复、Redis pending/retry/DLQ 与
PostgreSQL 终态对账、审计治理，以及队列/上下文/输出/重试/Artifact 容量上限为六个验收域。
2026-07-31，H0–H5 已在候选源码指纹
`627362f69997b2fd37334c7019033a72e940a9fc78ef700d3bc383af71d59328` 下全部通过。P2 工程验收完成；
详细证据真源见 `docs/21-p2-reliability-security-hardening.md`。

P3 只收口 Web Agent 控制台的产品体验，没有进入桌面封装或 Multi-Agent。顺序固定为：
Command Center 状态与恢复、权限影响范围与反馈、Timeline/Inspector/正文信息层级、窄窗口与长内容、
RAG 文档详情/版本/批量运维。P3-1 至 P3-5 已于 2026-07-31 完成实现、自动化回归和真实页面验收；
详细门禁见 `docs/22-p3-product-experience-closure.md`。

2026-08-01 已确认进入 P4。P4-1 多文档指定检索、综合回答、可信引用与 report/note 写入闭环已完成；
P4-2 已用 12 个 P0 回归题和 7 个扩展自然问题通过当前版本生产质量门禁。P4 不进入桌面端或
Multi-Agent；阶段设计见 `docs/23-p4-single-agent-knowledge-quality.md`，当前指标证据见
`docs/24-p4-rag-quality-baseline.md`。

P4-3 已把现有数据飞轮完整接入发布链路：生产 trace 自动采集、失败候选自动挖掘、脱敏审核队列、
confirmed 观察评估、版本化 promoted cohort、当前生产 Pipeline 自动重放和版本基线门禁。首版 10 条
固定回归集已通过真实重放；隐私批准、证据金标确认和晋升仍由人工负责，避免自动反馈污染回归集。
详细边界见 `docs/25-p4-rag-data-flywheel.md`。

P4-4 已把用户对助手回复的“有帮助 / 没帮助 / 依据不足 / 指定引用有误”接入同一数据飞轮。反馈由
持久化 Message 反查真实 Run 与最新 RAG trace，结构化候选进入 Workspace 审核队列；用户不能提交
trace_id，引用反馈必须命中本次 Context chunk。审核只标记候选已查看或忽略，不自动批准隐私、确认
证据标签或晋升固定 cohort。

P4-5 已把浅层“已查看”升级为受控诊断：审核者可查看候选、重排、Context 三阶段证据，标记失败类型，
并在 trace 隐私获批且没有人工/终态标签时选择正例与难负例生成 `user_feedback/draft` 标签。query 与
Chunk 摘要仅在隐私获批后展示；确认、晋升和发布门禁仍保持人工边界。飞轮报告同步汇总反馈状态和失败类型。

P4-6 把既有 CLI 人工流程接入 Knowledge 审核台：按 Workspace 查看 trace，完成隐私批准/拒绝、
`human_review` draft/confirmed/rejected 标签复核，并显式生成脱敏 promoted candidate。运行时晋升只更新
数据库标签并返回不含原文的候选；正式 `rag-promoted-p4-v1` cohort 仍由 release commit 固定，Web 与
Gateway 都不能直接改写 manifest 或执行质量门禁脚本。

P4-7 已完成质量发布收口：10 条固定 promoted cohort 在当前生产 Pipeline 上自动重放，代码质量与数据
质量共 11 项门禁通过。上一版指标已固化为只含聚合值的版本化基线；`rag/p4` 门禁默认自动比较，基线
缺失时失败关闭。当前不引入 BM25、Query Rewrite 或新模型，P4 至此完成。

P5-1 在不改变发布权限边界的前提下，把离线 RAG 门禁的脱敏聚合结果持久化并接入 Web 质量中心。
`release-gate.sh rag/p4` 仍是唯一执行入口；Gateway 只提供历史只读查询，页面不能执行脚本、修改基线/
cohort 或自动晋升样本。下一步在这组可观察结果上补质量趋势与定向失败簇治理，但任何门禁或金标变更
仍必须经过 release commit 与人工复核。

P5-2 在 P5-1 历史记录上增加确定性质量洞察：只比较相同 `gate_id + cohort_id` 的相邻运行，输出指标
改善/持平/退化、门禁失败或退化提醒，以及按阻断、上升、当前存在、已清零排序的失败簇。历史少于
2 次时明确返回 `insufficient_history`，不得画出伪趋势；失败簇只用于确定人工诊断顺序，不自动修改
检索策略、门禁阈值、baseline、cohort 或金标。

P5-3 把最新失败簇接回既有数据飞轮审核台：离线门禁持久化白名单化的 candidate/trace/query hash、
失败类型、阶段、严重度和指标 ID，质量中心按需回查当前 trace、隐私与标签终态并打开对应审核轨迹。
门禁记录不保存原始问题、答案、Chunk、向量或 Workspace；接口保持 GET-only。已 promoted 的目标明确
标记为固定回归样本，不重复晋升，也不由页面修改 cohort、baseline 或门禁。

P5-4 为脱敏失败候选增加独立质量治理状态：模块归属、出现次数、乐观版本和
`open → in_progress → resolved → verified` 闭环。`resolved` 表示等待下一次相同 gate/cohort 门禁；失败
未再出现时由门禁事务自动验证，再次出现时自动重开。人工操作写 AuditLog，但不修改 query、证据、
金标、cohort、baseline 或门禁阈值。

P5-5 用现有 `rag_quality_issues` 真源完成阶段台账收口：质量中心新增独立“问题台账”，按状态、责任
模块和失败类型有界筛选，持续展示首次、最近和验证 revision、出现次数、处理说明与记录版本，并能重新
打开原有人工审核轨迹。台账只返回 query hash 和结构化状态，不保存或展示原始 query、答案、Chunk 或
向量；问题状态 mutation 继续复用 P5-4 乐观版本与 AuditLog。至此 P5-1 至 P5-5 已形成
`门禁结果 → 趋势/失败簇 → 审核轨迹 → 问题治理 → 回归验证/长期台账` 完整闭环。

P6 优先进行 LangChain 接入和 LangGraph 编排收口，保留 P5 问题台账但暂不自动修改检索算法。
P6-1 已完成当前实现审计：LangGraph `StateGraph` 已在生产热路径中运行，模型调用、动作校验、工具执行
和恢复语义仍大量集中在 2181 行 `AgentRunner`。P6-2 已统一锁文件、项目 `.venv` 与生产 Conda 环境，
并以现有 `ModelProvider` 为边界接入供应商专用 LangChain adapter；`langchain` 为默认路径，旧 direct
Provider 保留为显式回退。P6-3 已完成：首个 `observe_result` 节点已迁入独立
`ObservationPhase`；第二个 `call_model` 节点也已迁入独立 `ModelCallPhase`，图状态更新已收紧为
typed transition；第三个 `validate_action` 节点已迁入 `ActionValidationPhase`，并以结构门禁限制为
`assess-only`；第四个 `execute_tool` 节点已迁入 `ToolExecutionPhase`，成为唯一受控 effect owner。
第五个 `extract_intent` 节点已迁入 `IntentExtractionPhase`；运行初始化和最大迭代终态已迁入
`RunLifecyclePhase`。价值审计确认 retry/call_model/end 等 7 组条件路由仍由 LangGraph 独占，
`AgentRunner` 从 2181 行缩减至 639 行。P6-4 已进入实施，第一切片补齐 Permission checkpoint 与
PostgreSQL request/task/run/step/tool-call/tool-name 的 effect 前身份对账；第二切片补齐 failed Run 与
开放 Step/ToolCall 的事务收口，并区分安全 continuation 与 effect unknown。下一步继续覆盖过期、取消
和命令重投的完整事件对账。第三切片已完成过期 permission 与终态 cancel 命令的幂等 ack；下一项是经
用户确认后，第四切片已为 ToolCall 权限状态补齐 `expired` migration、共享 DTO 和前端映射，消除终态
工具仍显示 pending 的语义缺口。P6-4 总恢复矩阵已完成，并决定不引入 LangGraph native interrupt/
checkpointer：跨 Worker 恢复继续以 PostgreSQL checkpoint v4 为唯一真源。P6-5 已完成首个可观察
切片：Timeline/Inspector 现在基于后端事件映射 retry、pause/resume 与安全检查点，不新增前端猜测
状态。第一轮真实长任务、权限接管、SSE 重连、取消与 Worker/RAG Worker/Redis 重启已执行；业务终态
和恰好一次副作用通过。pause/resume 恢复流一度会在终态后补发旧 `model.delta`；Gateway 现已完成
durable/ephemeral 有序合并、可选 sequence 投影和终态栅栏，并以同类真实旅程复验通过。P6-5 的
可观察映射、故障注入和真实长任务验收至此完成。完整阶段边界见
`docs/26-p6-agent-runtime-framework-consolidation.md`。

P6 固定顺序：

1. P6-1 当前实现、依赖、owner、安全边界与回归基线审计（已完成）。
2. P6-2 LangChain 模型 adapter 与结构化输出对账（已完成）。
3. P6-3 LangGraph phase service、typed state transition 与异步边界收口（已完成）。
4. P6-4 PostgreSQL checkpoint、权限 interrupt/resume 与故障恢复对账（已完成；native interrupt 保持关闭）。
5. P6-5 Timeline/Inspector 可观察映射和真实长任务验收（已完成）。

P6 不使用预制 agent 绕过 ToolGateway，不让 LangGraph checkpointer 成为第二业务真源，不进入
Multi-Agent、桌面封装，也不混入缺少门禁证据的 RAG 算法优化。

P7 在 P6 完成后进入工程化与发布产品化，不增加新的 Agent 智能能力。固定顺序是：RC2 发布基线与
CI、首次启动与配置自检、migration/备份/恢复升级体验、运行诊断与脱敏支持包、最终 RC2 候选验收。
P7-1 已建立统一 `ci`/`rc2` 门禁、结构化 JSON 报告和 GitHub Actions；完整边界见
`docs/27-p7-engineering-release-productization.md`。P7-2 已把结构化 preflight 接入既有 `dev.sh doctor`
和 `start`：启动前统一检查系统/项目依赖、生产模型与 RAG 配置、Workspace/Artifact 边界、可选 Runtime
和端口，输出 `ready/degraded/blocked` 脱敏报告。P7-3 已建立统一数据生命周期入口：备份、隔离恢复、
全量 public 表对账和显式升级串成失败关闭链路，普通启动不再隐式修改 schema。P7-4 已建立本地运行诊断
与脱敏支持包：只聚合健康、容量、日志级别/位置和操作证据，不打包原始日志或业务数据。P7-5 已完成
RC2 候选记录入口及同 revision 证据校验；最终候选记录须在该入口提交后，以新的干净 revision 重跑生成。

P7 固定顺序：

1. P7-1 RC2 发布基线、结构化门禁报告与 CI。
2. P7-2 首次启动、配置校验与依赖自检（已完成）。
3. P7-3 migration、备份、恢复与升级操作产品化（已完成）。
4. P7-4 运行诊断、脱敏支持包与故障定位入口（已完成）。
5. P7-5 同一 revision 的 RC2 候选验收与发布记录。

P7 不进入桌面封装、Multi-Agent、插件市场或缺少质量台账依据的 RAG 算法修改。

## Phase 0: 文档和设计

目标：

- 明确项目定位。
- 明确整体架构。
- 明确技术栈。
- 明确 Vue Web、Go Gateway / Runtime Orchestrator、Redis Runtime Bus、Python Agent Worker Pool、后续桌面 App、权限、上下文和 multi-agent 边界。

交付：

- 文档集初版。
- 核心对象模型草案。
- MVP 范围确认。

## Phase 1: 契约、骨架和 Mock 闭环

目标：

让用户可以在 Vue Web 控制台中输入任务，并通过 Go Gateway / Runtime Orchestrator 触发 Python mock Agent Worker，看到一次完整模拟运行。

交付：

- Vue 3 + TypeScript + Vite Web App。
- Naive UI 基础主题和主布局。
- Pinia / API client / Runtime event client。
- Go Gateway / Runtime Orchestrator 骨架。
- Runtime command / event envelope。
- Redis Runtime Bus 或同接口 in-memory bus。
- Python mock Agent Worker。
- OpenAPI / JSON Schema / DTO 契约。
- Dev Console。
- Runtime event fan-out。
- Agent Run Timeline UI。

验收标准：

```text
用户输入任务
-> Vue 调 Go Gateway createTask
-> Go Gateway 校验并初始化 Task / AgentRun
-> Go Orchestrator 入队 AgentRun
-> Python mock worker 消费 run job
-> mock event stream 返回 task.created / agent.run.started / model.delta / agent.run.completed
-> Go 消费事件并扇出给 UI
-> UI 实时显示步骤
-> Dev Console 可查看原始 DTO、命令和事件
```

## Phase 2: Permission Required 垂直切片

目标：

让 Agent 需要权限时，用户可以通过 Vue Web 接管，Go Gateway 将决策写入 worker command stream，Python worker 恢复运行。

交付：

- PermissionRequestDTO / PermissionDecisionDTO。
- Permission API。
- 权限确认弹窗。
- Inspector Permissions tab。
- Python PermissionManager mock / minimal version。
- Redis permission.required event 和 permission decision command。
- Go Gateway permission command router。
- Dev Console permission tester。

验收标准：

```text
Python worker 发出 permission.required
-> Redis runtime event stream
-> Go Gateway 扇出 RuntimeEvent
-> Vue 展示权限弹窗
-> 用户 approve / deny
-> Go Gateway 写入 resolvePermission command
-> Python worker 继续或拒绝
-> UI 展示 permission.resolved
```

## Phase 3: Tool Call Timeline 与 mock tools

目标：

让前端能看懂 Agent 调用了什么工具、参数是什么、结果是什么。

交付：

- ToolCallDTO / ToolResultDTO。
- Tool call timeline card。
- Tool detail / error state。
- Python ToolGateway mock tools。
- Redis tool events。
- Go Gateway tool event fan-out。
- Dev Console tool scenario。

验收标准：

```text
Python mock worker 请求工具
-> ToolGateway mock executor 返回成功或失败
-> tool.call.started / finished / failed 事件进入 Redis
-> Go Gateway 扇出事件
-> Vue Timeline 和 Inspector 正确展示
```

## Phase 4: Storage Interfaces + Task Recovery

目标：

刷新页面后，任务、运行状态和 Timeline 不丢；worker 重启后可以依据 Storage 恢复运行状态。

交付：

- Python Storage interfaces。
- TaskStore / RunStore / StepStore / ToolCallStore / PermissionStore。
- listTasks / getTask。
- Task Dashboard。
- Task Detail restore。
- RuntimeEvent 持久化策略。
- Dev Console raw records viewer。

验收标准：

```text
创建任务并产生步骤
-> 刷新 Web 页面
-> Go Gateway 查询 Storage / approved service boundary
-> UI 恢复 Task Dashboard、Timeline、Permission 状态
-> Redis 短期状态丢失不影响已持久化历史
```

## Phase 5: LangGraph AgentRunner 最小循环

状态：**已完成基础 single-agent 编排（2026-07-20）**。`AgentRunner.run()` 已接入
LangGraph `StateGraph`；当前先稳定单轮迭代、权限暂停、终态和最大迭代路由，后续再按
业务价值细分模型、工具、验证节点。

目标：

从 mock run 过渡到真实 Python Worker Runtime loop 骨架。

交付：

- LangGraph graph: build_context / call_model / decide / tool / verify / finish。
- LangChain mock model adapter。
- Worker run consumer。
- Command handler: pause / resume / cancel / permission decision。
- EventBus 到 Redis runtime event stream。
- 状态流转：running / waiting_for_permission / completed / failed。
- Go Gateway event fan-out 保持不变。

验收标准：

```text
用户创建任务
-> Go Orchestrator 入队
-> Python worker 启动 LangGraph loop
-> mock model 决策
-> mock tool 执行
-> RuntimeEvent 完整映射
-> UI 无需修改即可展示
```

## Phase 6: Real File Read + Real LLM Provider

状态：**MVP 主链路已完成**。`workspace.read_file`、`workspace.create_file`、`workspace.search_files`、`workspace.get_file_info`、OpenAI-compatible Provider、ActionParser、PromptBuilder、ModelMessage、多轮上下文和 ToolCall/AuditLog 持久化均已接入主链路。2026-07-16 已通过真实网页完成读取与创建验收；2026-07-17 已完成名称搜索和元信息查询的真实网页验收。

目标：

接入第一个真实本地能力和真实模型。

已完成（Phase 6A/6B/6C）：

- workspace.read_file + workspace scope 校验 + 文件大小限制。
- workspace.search_files：L0 递归名称/路径 substring 搜索，目录 FD 无 symlink 遍历，具备结果、扫描和深度上限。
- workspace.get_file_info：L0 单路径有限元信息查询，不读取正文、不跟随 symlink，只返回相对路径、类型、普通文件大小和修改时间。
- workspace.create_file：L2 新建 UTF-8 文件，不覆盖、不自动创建父目录，执行前 `allow_once`；
  成功结果在同一持久化事务内投影为 `deliverable/tool` Artifact。
- AgentActionParser（JSON → AgentAction 校验）。
- PromptBuilder + ModelMessage（供应商无关消息契约）。
- 供应商 ModelProvider 主链路：DeepSeek 独立 Provider + 可复用 OpenAI-compatible
  协议实现 + Provider Registry（配置、httpx、错误/重试与真实 API 验收）。
- Web 工作区选择（只消费服务端允许列表）与 Control Plane `WorkspacePolicy` 强制校验。
- 真实 LLM 自主选择 `workspace.read_file`，ToolGateway 执行后继续模型循环并生成最终回复。
- ToolCall、ExecutionStep、RuntimeEvent 和 AuditLog 持久化；刷新后 Timeline/Inspector 可恢复。
- Inspector 工具卡片展示工具名、provider、风险、参数摘要、结果摘要、耗时、错误和有限内容预览。
- 模型配置状态页与安全连通性测试。
- OpenAI-compatible SSE 流式 final_message，经 `model.delta` 实时显示在对话区。
- AuditLog 查询页：按事件类型、actor、Task/Run 筛选，游标分页；Web 只接收后端脱敏的只读安全投影。

后置兼容工作：LangChain adapter。LangGraph 节点与 PostgreSQL Run checkpoint 对账已完成，
PostgreSQL 保持真源；审计导出、保留策略、权限边界和实际清理演练已在 P2 完成。

验收标准：

```text
用户从服务端允许列表选择工作区
-> Python PermissionManager 判断风险
-> ToolGateway 读取文件
-> OpenAI-compatible model 总结内容
-> UI 展示工具调用、模型输出和审计摘要
-> 刷新后从 PostgreSQL RuntimeEvent 恢复
```

## Phase 7: MCP、Memory 和 Multi-Agent

目标：

扩展外部工具、长期记忆和复杂任务编排。

Memory 进度：Memory v1 的结构化正式记忆、Context 注入和管理页面已完成；Memory v2 已于
2026-07-26 完成收口，包括 Candidate/ExtractionJob 契约、持久化、原子批准/拒绝、Web
确认区、DeepSeek 异步提取、证据来源校验、有界重试与崩溃恢复、跨 Run pending 去重和独立
到期维护，并已通过真实端到端验收。语义/向量检索继续后置，并与 Obsidian 个人知识库保持
独立边界。个人知识库 v1、L2 Agent 写入工具和 daily/weekly 持久化定期任务已经完成；下一阶段
MCP v1 的 stdio 配置、发现、持久化、ToolGateway L3 执行和 Tools 页面已经完成。权威来源
第一切片也已完成：内置 arXiv 元数据 MCP、一键注册，以及 native L2 受控 PDF Artifact 下载。
定期来源报告现已具备服务端固定 arXiv 查询范围、1–10 条结果上限、已写入报告来源去重和 Obsidian
写入闭环；PDF 不由知识库自动下载。原内置 `knowledge-curator` 已撤下：研究任务由 LLM 在普通
Agent loop 中动态组合来源检索、Artifact 下载、个人知识库和 RAG 原生能力；Source Provider 明确返回
可下载性，Knowledge Service 在写入内部校验。通用 SkillScriptExecutor 作为后置、受信任扩展基础仍
保留，但 Skill 不拥有产品工作流或证据真相。RAG ingestion 已完成领域契约、可恢复状态机、
Store 端口、PostgreSQL 文档/作业/分块元数据与 Workspace 复合外键，并按
ingestion/preprocessing/chunking/embedding/indexing/retrieval/ocr 分包，完成 PyMuPDF
原生解析、统一多模态中间结构、页面路由、localhost MLX-VLM + PaddleOCR-VL-1.6 adapter、
Native/VL 融合和按模态分片。`RagIngestionService` 已连接受控 PDF Artifact、可恢复 lease/心跳、
Element/Asset/Chunk relation 原子持久化、失败重试、取消和审计，并停在不误标 ready 的 embedding
交接状态。OpenAI `text-embedding-3-small` Provider、1536 维 pgvector/HNSW adapter 和独立
`RagEmbeddingService` 已完成，成功时在同一事务内将 Job/Document 收敛到 `completed/ready`。
独立常驻 RAG Worker 已完成第一版装配：拥有单独配置、进程入口、PostgreSQL Job 轮询、公平阶段
调度、异常退避与优雅停止，并已接入统一开发启动脚本。在线检索与证据回答第一版也已完成：
`RagRetrievalPipeline` 已按 QueryRewriter/Retriever/Reranker/ContextAssembler 建立可替换边界，
Workspace/ready/provider/model 过滤、候选去重、相邻 Chunk、多模态 Element、Token 预算以及 L0
`rag.search` 均已接入 Agent；Prompt 可消费有界证据，Runtime 会拒绝伪造引用并用可信元数据生成引用。
当同一 Run 随后写入知识文档时，Runtime 还会把模型实际可见的 RAG Context Chunk 连接到
`rag.search` ToolCall、RAG Document 和来源 Artifact，并覆盖模型不能提交的 `provenance_links`。
Workspace-scoped RAG 文档管理读模型、Gateway API 与 Knowledge 页面状态区也已完成。关键词
Retriever + RRF 已完成默认装配；下一步接可选语义 Reranker 和报告阅读问答入口。用户显式 PDF 上传入口已完成：受控
Artifact、来源审计、幂等 RAG Job 与页面进度显示共用同一条入库链路。

RAG 后续 TODO 由 P4 质量门禁决定：通过既有端口接入 Query Rewrite；将关键词召回升级为正式 BM25；
使用已存向量改进 MMR 相似度；补充动态配额与更丰富 metadata filter；重审视觉增强预处理 v2
（OCR/视觉描述/缓存/路由）；继续完善 ContextAssembler 的冲突处理与 block/quotas。可信
`document_ids` 解析、本地 Cross-Encoder、文档详情/版本和批量运维已经完成，不再列为 TODO。

Runtime 的新 Run 已按全局单调序列生成互不碰撞的 Model/Tool Step ID；首次投影 Step 时在 Run 行锁
保护下原子分配连续 `order_index`、增加 `step_count` 并更新 `current_step_id`。只读业务真源对账会
报告历史 Run 的计数、顺序或 Event/Step 类型不一致，但不会静默改写既有审计事实。

Intent 后续 TODO（当前不实现）：用户后续重构 LLM Intent 时，拆分
source resolution、evidence requirement、required effects 与 download/ingestion policy，保留
advisory/required 差异，并为外部来源检索增加重复查询检测与有界搜索预算。

### 后置：通用 Skill 安装与供应链治理

本能力不属于当前 MVP，也不代表提前建设插件市场。当前只运行仓库内受信任 Skill；未来需要导入
第三方通用 Skill 时，再实现隔离下载/暂存、标准包校验、信任分级、依赖与 adapter 解析、用户能力
摘要、原子启用、版本/fingerprint 固定、更新复审、禁用与撤销。

第一阶段只承诺纯指令型 Skill 可以在 `restricted` 模式显式运行，不授予 Tool、脚本、Memory、后台
任务或自动持久化。完整能力只有在依赖和 Jarvis adapter 被可信解析后开放，且所有真实动作继续经过
ToolGateway、PermissionManager、AuditLog 和 RuntimeEvent。第三方脚本沙箱、签名发布者体系和市场
分发继续后置。

验收条件：

```text
未知来源 Skill 下载到隔离区
-> 安全与结构检查
-> 生成 trust / compatibility / capability summary
-> 用户明确批准允许的能力
-> 原子启用或保持 quarantined / needs_adapter
-> 可选 Skill 失败不影响 Worker 主运行面
```

交付：

- MCP server config（stdio v1 已完成；HTTP/SSE 后置）。
- MCP tool discovery（启动发现与手动刷新已完成；热重载后置）。
- MCP tool 通过 ToolGateway 执行（默认 L3 已完成）。
- Memory Store。
- Memory extraction / confirmation。
- LangGraph multi-agent task graph。
- Go Orchestrator 多 worker 并发调度。
- Coordinator / Worker / Reviewer / Synthesizer。

验收标准：

```text
复杂任务
-> Go Orchestrator 调度一个或多个 worker run
-> LangGraph 拆分任务图
-> Worker 调用 MCP / native tools
-> Reviewer 检查
-> Synthesizer 汇总
-> UI 展示任务图、工具调用和最终结果
```

## Phase 8: Audit / Recovery / Persistence Hardening

目标：

把系统从可演示变成可长期使用。

交付：

- AuditLog 完整化。
- run pause / resume / cancel（安全 checkpoint 的 pause/resume/cancel 已完成）。
- failed step retry（仅可恢复 `MODEL_CALL` 从 `call_model` 安全 checkpoint 创建 replacement Run，已完成；工具/未知结果不重放）。
- artifact store（最终 Markdown 产物、超阈值文本的受控本地文件存储、workspace 文件交付物
  的可信来源核对与安全按需预览、hash/size/mime 元数据、有界 RuntimeEvent 与刷新恢复已完成；
  截图及其他二进制对象仍待后续）。
- 错误日志。
- LangGraph checkpoint 与项目 Storage 对账（最近 Run 的 PostgreSQL Task/Run/Event/Step/Artifact
  只读一致性快照，以及 failed Run 缺失终态事件的 L3 单次受控补写已完成；checkpoint
  语义级差异与其他修复工具仍待后续）。
- Redis Run Queue、worker-command、runtime-event 的 pending / retry / dead letter / consumer group 恢复（基础闭环、Runtime Health、DLQ 脱敏查询与 Run Queue 受控重试已完成；RuntimeEvent 专用修复工具仍待后续）。
- worker heartbeat、孤儿 run recovery 和 Runtime Health 运营面板（健康摘要、DLQ 诊断、L3
  单次确认处置与 PostgreSQL 业务真源只读对账已完成）。
- 无界上下文防护。
- 超时、重试、取消机制。

RC1 后 P2 加固范围：

- 分别重启 Gateway、Agent Worker、RAG Worker 与 Redis 的故障注入；每次都验证 PostgreSQL
  权威状态、Redis 临时投影、Worker heartbeat 和副作用幂等。
- 长任务运行、SSE 主动断开/重连、Gateway 重启后历史补偿与终态一致性。
- Alembic 单 head/current 校验、PostgreSQL 备份、隔离临时数据库恢复和关键表精确行数对账。
- Run Queue、Worker Command、Runtime Event 的 pending、retry、DLQ、ACK 与 Run/Event 终态对账。
- AuditLog 安全导出、保留策略、导出权限边界和敏感字段脱敏回归（安全投影、流式导出、只读预演
  与 L4 单次确认的有界实际清理已完成；一次性临时 PostgreSQL 执行演练待最终 P2 汇总门禁）。
- Redis stream/PEL/DLQ、Agent 上下文、模型输出、重试次数、Artifact 与 RAG Asset 的显式容量预算。

P2 不以新增功能数量为完成标准，也不提前实现 Multi-Agent。每个域必须有自动化或可重复演练入口、
安全失败条件和本次 revision 的验收证据。

验收标准：

```text
长任务失败后可恢复
权限和工具调用可审计
刷新页面后状态一致
Redis 只作为运行时 bus，Storage 仍是业务真源
Go Gateway、Python Worker、Vue Web 的错误和事件契约一致
```

## Phase 9: Web 产品体验收口

目标：

在已通过 P2 稳定性与安全门禁的 Runtime 上，把 Web Agent 控制台收口为状态真实、错误可恢复、
权限影响可理解、长内容和窄窗口可用的个人控制台。

交付：

- Command Center 全状态中文表达、SSE 连接状态和原地恢复操作。
- 任务创建失败保留输入草稿；统一展示安全错误码、可恢复性和重试入口。
- Permission Dialog 的影响范围、参数摘要、授权有效期和决定反馈。
- Timeline、Inspector 与对话正文的信息层级和去重。
- 窄窗口、长文本、代码、表格、路径和 Artifact 展示。
- RAG 文档详情、版本信息、批量启停/重试/删除的受控运维入口。

验收标准：

```text
RuntimeEvent / AppError / PermissionRequestDTO / RAG DTO
-> Frontend State 形成唯一 UI 投影
-> Command Center 不暴露原始枚举、不伪造连接或结果
-> 失败保留用户输入并提供契约允许的恢复动作
-> 权限、长内容和窄窗口在真实页面可理解、可操作
```

## Phase 10: 单 Agent 知识研究质量收口

目标：

让指定多份资料的检索、比较、引用和 report/note 写入成为可量化、可解释、可恢复的真实产品闭环。

交付：

- 多文档可信范围与证据覆盖。
- 覆盖不完整时的安全降级表达。
- 多文档 RAG -> Knowledge provenance。
- 当前 revision 的生产 RAG 质量基线与失败分类。
- 只根据质量证据进入 Query Rewrite、BM25、向量 MMR 或 Context 策略调整。

详细门禁见 `docs/23-p4-single-agent-knowledge-quality.md`。

## Phase 11: 桌面体验增强（后置）

目标：

在 Web 端交互与 Runtime 能力完成 P3 收口后，再将稳定 UI 和 Runtime 能力封装成桌面端。本阶段
当前不启动。

交付：

- 桌面端 shell。
- preload / IPC adapter。
- 菜单栏常驻。
- 全局快捷键。
- 本地通知。
- 后台任务。
- 剪贴板工具。
- 截图工具。
- 本地模型支持。

验收标准：

```text
用户可以用快捷键唤起 App
后台任务可继续运行
系统通知提醒用户确认或查看结果
```
