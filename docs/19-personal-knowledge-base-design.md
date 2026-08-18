# 个人知识库设计

## 定位与边界

个人知识库与 RAG 是两套可独立运行、也可组合的系统：

- 个人知识库以 Obsidian Markdown 为内容真源，面向人阅读、编辑、链接和长期整理，不做向量化。
- RAG 以后保存文献分块与向量索引，面向模型检索和领域解释，不替代 Markdown 报告。
- 定期任务可以生成报告写入个人知识库，并把原始文献交给 RAG；报告中的问题再通过 RAG 检索解释。

## Vault 隔离

当前使用独立 Vault，而不是在既有个人 Vault 中创建子目录：

```text
~/Documents/obsidian/
├── knowledge/   # 用户已有 Vault，Jarvis 默认不读写
└── Jarvis/      # Jarvis 专用 Vault
    ├── .obsidian/
    ├── Reports/
    ├── Notes/
    ├── Sources/
    └── 索引.md
```

默认路径可由 `JARVIS_OBSIDIAN_VAULT_PATH` 覆盖。注册时必须经过 Workspace 路径安全策略，目录名必须为
`Jarvis`；每次写入前重新验证 canonical root，拒绝符号链接替换。Web 页面只能显式连接和创建文档。
连接新路径会原子切换唯一当前 Vault，旧 Vault 仅标记为停用，不删除文件、索引或历史元数据；重新连接旧
路径可以恢复它。Agent 因而不会在多个 active Vault 之间猜测写入目标。
Agent 和定期任务通过已实现的 L2 `knowledge.create_document` 写入，不能直接调用本服务绕过权限、审计和事件链路。

## 内容与索引

Markdown 文件是正文真源；PostgreSQL 只保存 Vault 注册和 Jarvis 创建文档的元数据、哈希、大小与来源。
文件名由服务端根据纯语义标题生成，不包含 UUID 或 Run ID，客户端不能提交相对路径。单文件上限 512 KiB，创建禁止覆盖；
`索引.md` 是 Jarvis 自有文件，通过同目录临时文件原子替换，内容使用 Obsidian wikilink。

首版 frontmatter 包含 `jarvis_id/title/type/tags/sources`。`Sources/` 只保存来源说明。当前已新增
独立的 arXiv 元数据 MCP 与受控 PDF Artifact 下载工具；PDF 进入 Artifact Store，不进入 Obsidian
Vault。向量化仍属于后续 RAG ingestion，不在个人知识库内隐式执行。

RAG ingestion 已建立独立的 `RagDocument`、`RagIngestionJob`、`RagChunk` 领域契约和 PostgreSQL
元数据表。它只引用受控 Artifact，不读取 Obsidian Markdown，也不会在知识库写入后隐式启动。
图片、图表、表格和公式使用独立 `RagElement/RagAsset`，通过显式 relation ID 与文本 Chunk 关联，
避免在文本记录中嵌入二进制内容。当前已增加 PyMuPDF 原生解析、本地 MLX-VLM +
PaddleOCR-VL-1.6 多模态预处理和按模态分片；受控 PDF Artifact 可由独立 RAG application service
校验血缘后持久化 Chunk/Element/Asset/Link，处理不会读取或改写 Obsidian。百度智能云 OCR 只保留为
默认关闭的可选 fallback。OpenAI Embedding 与 PostgreSQL/pgvector 向量索引现已作为独立 RAG
阶段实现，只处理受控 RAG chunks，不读取或改写 Obsidian；这不改变 Obsidian 作为人类可读知识
真源的边界。上述处理现由独立 RAG Worker 执行；它与生成 Obsidian 报告的 Agent Worker 分离，
因此可以单独重启、限制为单并发并恢复 lease，视觉解析失败也不会阻塞普通 Agent Task。
PDF/视觉抽取文本在进入 PostgreSQL 前会移除其 text/JSONB 不支持的 `U+0000`；正文、结构化字段、
Chunk hash、token 与确定性身份以清理后的内容为准。该兼容处理属于 RAG 摄取链路，不修改原始 PDF
Artifact，也不影响个人知识库 Markdown。
Agent 可先用 L2 文献下载工具生成受控 PDF Artifact，再经另一项 L2 `rag.ingest_artifact` 显式入队；
两次权限互不继承。入队工具只返回作业信息，独立 Worker 完成处理并把文档标为 `ready` 后才可检索。
若用户要求在真正可检索或向量化完成后再告知，LLM 根据入队 ToolResult 的 `job_id` 继续选择 L0
`rag.await_ingestion`；该工具只读等待真实 job/document 终态，不执行解析、向量化或重试。若用户只
要求提交后台处理，Agent 可以在成功入队后结束并如实说明仍在处理中。

在线问答使用 `RagRetrievalService` 读取 RAG，而不是检索 Obsidian Markdown。Agent 可通过只读
`rag.search` 获取带文档、页码、Chunk、Element 与 Asset ID 的证据；未来报告阅读页也可以通过 API
调用同一 Service。两种入口共享 Workspace 过滤和 Context Package，不建立第二套检索逻辑。检索
内部通过 Rewrite/Retrieve/Rerank/Assemble 四阶段 Pipeline 扩展；回答中的引用由 Runtime 根据可信
Chunk ID 补全文档、页码和 Artifact 元数据，不能由模型自行声明。

用户在下一轮要求把“刚才”的研究写入知识库时，模型可看到最近对话文本，但可信来源走独立侧链：Runtime
只从有界历史中最近一个完整 assistant turn 的 `run_id` 读取已完成 ToolCall，并按与当前 Run 相同的规则
重建 provenance。Intent 负责结构化声明来源是 `skip/optional/required` 以及用户是否给出原样标题；来源
为 required 却无法从当前或最近历史 Run 恢复时，在权限请求前失败关闭。正文里的文件名、页码或模型生成
UUID 永远不能替代该关联。

## 定期任务

计划和执行实例保存在 PostgreSQL。daily/weekly 到期或手动触发时创建普通 Task/Run，通过现有
Outbox、Redis 和 Worker 执行。普通报告只授权知识库写入；`source_report` 还可授权第一方 native
arXiv 元数据检索，并将 query 与 1–10 条结果上限固化为服务端 `source_policy`。计划不能授权
Workspace 写入、通用 MCP 或 PDF 下载。计划可暂停/恢复，页面刷新和进程重启后均可恢复。

定期来源报告会过滤同一计划已经成功写入文档 `source_urls` 的 arXiv 来源，再把新结果总结到
Obsidian。`source_urls` 由 Runtime 从可信检索 observation 覆盖写入，不由模型自由声明。通用
arXiv MCP 仍需 L3 单次确认，PDF 下载仍需 L2 单次确认；RAG 入库继续作为下一套
独立系统，不因生成报告而自动触发。定期任务产物也不进入 MemoryExtractor：知识库负责保存报告，
任务计划负责保存周期指令，避免把自动生成内容误判为用户长期记忆。

## Agent 组合两条独立链路

研究任务不是 Skill 工作流。LLM 在普通 Agent loop 中根据用户目标和每轮 ToolResult 决定下一步，
包括查询改写、来源相关性、是否继续检索、如何总结以及何时写入知识库。Runtime 不按固定阶段替 LLM
推进，也不因为目标包含“知识库”或“RAG”就加载专用 Skill。

是否可下载不是 LLM 的语义判断。来源 provider 通过 `RetrievedSourceDTO` 返回：

```text
source_id / source_type / title / canonical_url
content_scope / content_text / content_locators / content_sha256
download.available / download.reference / download.mime_type / download.url
```

`download.available` 只能来自 provider 对真实来源的确认。LLM 负责判断来源是否与目标相关；当用户
要求“下载所有可下载的相关资料”时，所有相关且 `download.available=true` 的来源都应进入下载调用，
不能再按主观价值二次筛选。只有用户明确要求“选一篇”或“只保存重要资料”时，LLM 才进一步做语义选择。

原文下载成功后得到受控 Artifact。随后同一 Task 可以分别调用：

```text
knowledge.create_document  -> 人可读 Markdown，个人知识库链路
rag.ingest_artifact        -> 异步 RagIngestionJob，RAG 链路
rag.await_ingestion        -> 可选等待真实 ready，仍属于 RAG 链路
```

两条链路共享 `task_id/run_id/source_id/artifact_id` 血缘，但生命周期独立。知识文档不等待 RAG ready；
RAG Worker 不读取或改写 Obsidian。写知识文档不自动触发 RAG，提交 RAG 也不自动写知识文档，两项
L2 权限分别确认。某条链路失败时，另一条已经成功的产物继续保留并在 Timeline 中如实展示。

生产 Agent 先通过普通 LangGraph `extract_intent` 节点识别两条独立 effect：
`knowledge_write` 与 `rag_ingestion` 各自为 `skip/optional/required`，它们只约束后续 Agent loop，不能
直接执行服务。LLM Intent 候选通过严格 Schema 与 Runtime 能力校验后，必需 effect 才会进入 finish
证据门控；所有真实写入仍分别经过 ToolGateway、Permission、ToolCall、AuditLog 和 RuntimeEvent。
文档问答另由 Task/Workspace scoped 的匿名 ready 文档目录解析范围，Runtime 覆盖
`rag.search.document_ids`；目录提供有界首 Chunk 身份摘要帮助把自然语言主题映射到真实文档，明确
名称与所选文档不一致时 Runtime fail closed。Intent 模型不能读取或伪造数据库 UUID，也不能用相近
论文替代用户点名的资料。是否下载仍完全取决于来源 ToolResult 的 `download.available`，不由 Intent
LLM 猜测。

当用户点名多份 ready 文档时，Runtime 将 `rag.search.top_k` 至少提升到已选文档数，最终检索策略在
候选可用时优先保留每份文档的一条证据，并返回 `document_coverage`。覆盖不完整不阻止展示已有证据，
但 Agent 必须明确范围缺口，不能把部分文档结果写成全面比较。随后写入 Knowledge 的 report/note
标题应概括共同主题、比较问题或报告周期，不拼接来源文件名；provenance 仍逐条连接实际可见 Chunk。

`KnowledgeApplicationService` 在写入内部校验标题、正文、kind、标签、来源 URL 和可信 provenance，
不要求模型先调用额外的 validate Skill 工具。Runtime 从本 Run 成功的来源检索、下载、ingestion 和
RAG search ToolResult 连接 canonical source、Artifact UUID/SHA-256、RAG document/job/status，以及
检索 ToolCall 与实际进入模型上下文的 Chunk，并覆盖模型无法提交的 `provenance_links`。文件适配器把
它追加为 Markdown `Jarvis Provenance` 表；异步入库
尚未 ready 时只能记录当前 queued/pending 状态，不能宣称索引完成；只有可信等待结果
`ready=true` 才能在本次任务中宣称向量化完成。

`rag.search` 关联只处理当前 Run 成功的 native ToolResult 和 Prompt 实际可见的 nested context Chunk；
UUID 严格校验、稳定去重并限制最多 50 项。它表达“生成该文档时可见哪些检索证据”，不能被描述为
正文逐句引用；没有可信字段时不得猜测 URL、job 或 ingestion status。

仓库已撤下 `knowledge-curator` 包和 Jarvis adapter。通用 Skill 基础设施只用于未来可选的领域方法论，
不得拥有搜索、下载、知识写入、RAG、权限、任务状态机、阶段工具可见性或证据真相。
