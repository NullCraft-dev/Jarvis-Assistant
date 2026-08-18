# P3 Web 产品体验收口

## 目标与边界

P3 在 P2 可靠性与安全门禁通过后进行，目标是把当前 Web Agent 控制台从“链路可用”收口为
“状态可信、恢复明确、信息易读、窄窗口可用、日常运维可操作”。

P3 不新增桌面 shell，不提前建设 Electron / IPC，不进入 Multi-Agent，也不借 UI 改动新增绕过
ToolGateway、PermissionManager、Storage、AuditLog 或 RuntimeEvent 的执行路径。

## 实施顺序

### P3-1 Command Center 状态表达与错误恢复

- `AgentRunStatus` 的 12 个契约状态必须全部映射为面向用户的中文标签和说明，不直接暴露枚举值。
- Header 的连接状态必须来自 Worker 查询和 active Run 的 EventSource 生命周期；禁止固定显示
  “已连接”。
- EventSource 对外投影 `connecting / open / reconnecting / closed`。瞬时错误继续交给浏览器携带
  `Last-Event-ID` 自动重连；用户可主动重建订阅。
- 终态错误只展示 `AppError.code/message/recoverable`，不得展示 `details`、原始异常或堆栈。
- 只有后端契约确认安全的失败步骤才提供“从安全检查点恢复”。
- 创建 Task 失败时保留 Composer 草稿；只有创建成功并开始订阅后才清空。
- fetch 拒绝统一归一为可恢复 `NETWORK_UNAVAILABLE`，UI 提供原地重试。

状态：**已完成首轮实现与真实页面验收（2026-07-31）**。

### P3-2 Permission Dialog 影响范围与授权反馈

- 明确展示工具名、风险等级、动作摘要、参数安全摘要、Workspace/Path/MCP scope。
- 每个允许决定必须说明有效范围和持续时间；L4 只允许单次确认，L5 不提供批准。
- 提交中、已接受、已拒绝、已过期、冲突和网络错误在同一请求卡片内反馈。
- 反馈继续只消费 `PermissionRequestDTO`、resolve acknowledgement 和 durable
  `permission.resolved/expired`，不由前端伪造 Worker 已恢复。

状态：**已完成首轮实现与真实权限拒绝验收（2026-07-31）**。

### P3-3 Timeline / Inspector / 对话正文信息层级

- 对话正文优先表达目标、结果和交付物；Timeline 表达过程；Inspector 表达审计与解释。
- 同一事实不在三处等权重复；折叠项必须有清楚摘要。
- 原始状态枚举、事件类型和内部 ID 只在明确的技术详情层出现。
- 工具、权限、模型重试和失败原因能从摘要逐层展开。

状态：**已完成首轮实现与真实信息层级验收（2026-07-31）**。

### P3-4 窄窗口与长内容

- 覆盖宽屏、窄桌面和最小支持宽度；侧栏与 Inspector 不挤压主操作区。
- 长路径、代码块、表格、引用、错误消息和 Artifact 卡片必须有界换行或横向滚动。
- Composer、权限主操作和错误恢复入口在窄窗口仍可见。
- 用真实渲染验证，不只依赖类型检查或 DOM 单元测试。

状态：**已完成首轮实现与真实窄窗口/长内容验收（2026-07-31）**。

### P3-5 RAG 文档详情、版本与批量运维

- 文档详情展示可信来源、状态、版本、解析/Embedding 标识、Chunk/向量计数和最近 Job。
- 版本冲突使用服务端 `expected_version` 结果，不在前端覆盖。
- 批量启停、重试、取消和删除必须有选择上限、影响摘要、部分失败结果和权限边界。
- 删除继续走既有受控 Permission 流程；前端不得直接改数据库或文件。

状态：**已完成实现、自动化回归与真实页面验收（2026-07-31）**。

## P3-1 验收记录

- Web 生产构建通过。
- Web 测试 `21 files / 81 tests passed`。
- 真实运行栈下完成 1440×900 与 760×900 页面检查。
- 历史 failed Run 可从 PostgreSQL/SSE 恢复，页面显示中文“运行失败”、安全错误码
  `RUN_QUEUE_RETRY_EXHAUSTED` 和不可恢复说明。
- Header 由固定“已连接”改为真实“正在检查 / 服务正常 / 服务不可用 / 事件连接中 /
  事件重连中 / Worker 离线”。
- 浏览器 console 的 warning/error 为 0。

## P3-2 验收记录

- Permission presentation 收口到单一前端 owner，统一解释风险等级、动作、影响范围、
  `allowed_decisions`、scope facts、参数安全摘要和决定结果；UI 不再直接展示后端枚举值。
- 当前 Run 的 pending Permission 固定进入 Command Center 底部“当前权限接管”区域，不再被长
  Timeline 推到视口之外；Timeline 保留过程证据，Inspector 只展示审计摘要。
- 决定按钮只来自服务端 `allowed_decisions`；前端额外 fail closed：L4 不展示持久授权，L5
  不展示任何批准决定。
- 请求提交中禁止重复点击；网络、冲突和过期错误留在对应请求卡片。决定已被接受只表示授权等待
  结束，明确不伪造工具或 Run 已成功。
- Permission 参数按行数和值长度有界展示；内容正文只显示 byte size 与 SHA-256 摘要，对疑似敏感
  key 做防御性脱敏。
- Web 生产构建通过；Web 测试 `23 files / 88 tests passed`。
- 在真实运行栈完成 1440×900 和 760×900 检查。L2 `workspace.create_file` 请求在窄窗口仍能完整
  查看影响范围并点击拒绝；随后 durable Timeline 收口为
  `permission.resolved(deny) -> tool.call.failed(PERMISSION_DENIED) -> agent.run.failed`。
- PostgreSQL 权威 Task/Run 状态为 failed，目标 `tmp/p3-permission-denied.txt` 未创建；安全审计投影
  同时包含 `permission.required`、用户 `permission.decision=deny` 和
  `tool.call.failed/PERMISSION_DENIED`，`content` 仍为 `[已脱敏]`。
- 本次真实权限旅程的浏览器 console warning/error 为 0。

## P3-3 验收记录

- 新增统一 RuntimeEvent presentation owner，把事件转换为面向用户的标题、摘要、类别、语气和
  Inspector 下钻目标；Timeline 不再各自读取任意 payload 或把事件枚举当标题。
- 对话正文只承载用户目标与 Agent 结果；`final_response` Artifact、`model.delta`、上下文预算和原始
  log 不再进入 Timeline。deliverable Artifact 仍保留“已保存”过程证据，完整卡片继续由交付物区域
  展示。
- Timeline 将同一次 model/tool/MCP 的 started + terminal 折叠为终态节点；已处理 Permission 只保留
  resolved/expired，隐藏对应 required。历史 Run 默认折叠，新建 Run 执行中自动展开，进入终态后
  自动折叠并把视觉焦点还给对话结果。
- InlineRunBlock 头部只展示中文运行状态和关键节点/工具/权限/失败计数，不再显示原始状态和总事件
  数；失败、工具和权限节点可直接打开对应 Inspector。
- Context Inspector 只展示 Workspace、上下文预算、历史/观测/记忆裁剪和 Skill 身份，不重复任务
  标题或用户目标。Permission Inspector 按 request id 合并为最新状态，不同时展示 pending 和历史
  required。
- 原始 RuntimeEvent type、event id 和 step id 只出现在明确标注的“技术诊断层”，并且需要展开单条
  记录后才显示；第一层继续使用用户可理解的摘要。
- Web 生产构建通过；Web 测试 `25 files / 97 tests passed`。
- 真实运行栈完成历史失败 Run、实时成功 Run，以及 1440×900、760×900 渲染检查。历史 12 个
  RuntimeEvent 收口为 6 个关键节点；实时无工具 Run 完成后正文显示最终回复，Timeline 收口为
  4 个关键节点。浏览器 console warning/error 为 0。

## P3-4 验收记录

- `1024px` 以下不再让 200px Sidebar 或 320px Inspector 挤压 Command Center；两者改为互斥的
  overlay drawer，保留各自的宽屏开合偏好。Header 的检查器按钮读取当前布局的真实开合状态，
  不再出现“面板已隐藏但按钮仍提示隐藏”的错位。
- Header、Conversation Thread、消息气泡、Composer、Run 状态/恢复区、任务创建错误区、权限接管区
  和 Artifact 卡片补齐 `min-width: 0`、响应式间距、换行与操作区换行规则。480px 下主要输入与
  恢复操作不会被侧栏或长文字挤出视口。
- 用户文本、Markdown 段落、引用、链接、行内代码、错误消息和 Workspace 路径允许有界断行；
  代码块保留内部横向滚动；宽表格为单元格设置可读最小宽度并只在消息内部滚动；图片不超过消息
  宽度；Artifact 长标题截断并保留完整 title，正文与读取错误保持有界。
- 新增 compact panel store、Run 恢复操作区，以及长 Markdown/Artifact 边界回归。Web 生产构建
  通过；Web 测试 `27 files / 105 tests passed`。
- 真实运行栈完成 1440×900、760×900、480×820 三档检查；导航与检查器抽屉可打开、关闭且不改变
  主内容宽度。实时无工具 Run 返回超长路径、180+ 字符代码行、五列表格和引用：路径有界换行，
  代码与表格显示局部滚动，Composer 与终态区保持可见；浏览器 console warning/error 为 0。

## P3-5 验收记录

- RAG 文档第一层直接展示后端权威状态、文档 `version`、ingestion policy、受控 Artifact 来源、
  Chunk/向量计数、最新 Job 阶段/尝试/执行器/真实进度和更新时间。展开层补充 Document/Artifact/Job
  ID、Parser、Chunker、Embedding provider/model/dimensions、索引目标和完整生命周期时间。
- 向量计数只消费 `latest_job.progress`；旧 completed Job 没有进度快照时明确显示“历史作业未记录”，
  不用 Chunk 数伪造向量完成数。stale、失败码和视觉路由继续使用后端 DTO。
- 单项与批量 mutation 均携带对应文档快照的 `expected_version`。服务端返回
  `RAG_DOCUMENT_VERSION_CONFLICT` 或 `RAG_RESTART_CONFLICT` 时，前端保留安全 AppError 并刷新
  Workspace 权威列表，不覆盖新版本。
- 批量选择硬上限为 20。启用、停用、重新执行和取消按选中顺序调用既有单文档受控 API；执行前
  展示影响、适用数与跳过数，执行后逐项保留成功、失败、跳过和安全错误码，允许部分失败。
- 批量永久删除不新增宽松后端接口，也不一次性批准。前端按顺序为每份文档创建既有 L4
  PermissionRequest；用户必须逐项允许、跳过或结束，原始 Artifact 始终保留。
- Web 生产构建与 `git diff --check` 通过；Web 测试 `29 files / 115 tests passed`。真实运行栈读取
  19 份 PostgreSQL RAG 文档，在 1440×900、760×900、480×820 检查详情、批量停用预览和批量删除
  权限提示；未执行真实状态修改，页面无横向溢出，浏览器 console warning/error 为 0。

## 完成标准

P3-1 至 P3-5 已完成自动化检查、宽屏/窄屏真实渲染检查，并把关键结果回写
`docs/12-development-progress.md`，因此 P3 Web 产品体验收口于 2026-07-31 完成。P3 完成不自动授权
进入桌面端；桌面阶段仍需用户明确决定。
