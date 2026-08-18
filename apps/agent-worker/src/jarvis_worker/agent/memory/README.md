# Memory

`memory/` 负责长期记忆模型、CRUD 服务、Repository 端口、PostgreSQL adapter，以及
向 ContextManager 提供的有界记忆读取。

Memory 的业务与数据访问代码在这里聚合；公共数据库连接和事务仍由
`database/` 提供。任务恢复、运行状态和审计真相不属于 Memory。

Memory v2 将模型输出限定为 `MemoryCandidate`。`MemoryExtractor` 只能返回候选规格；
候选经过结构校验、敏感检查、去重和 workspace 边界检查后才能保存为 `pending`，且永远
不会被 ContextManager 读取。只有用户批准后，在同一 PostgreSQL 事务中创建的正式
`Memory(status=active, source_type=candidate_approved)` 才能进入后续上下文。

异步提取使用独立 `MemoryExtractionJob` 持久化状态，不向已经完成的源 AgentRun 追加
RuntimeEvent。成功 Run 在终态事务中幂等创建 Job；Worker 后台执行器从 PostgreSQL 领取、
重试并恢复 stale 作业。第一版 `DeepSeekMemoryExtractor` 只返回严格 JSON 候选，来源 ID、
workspace、敏感检查、去重和持久化仍由 Application Service 掌握。`memory-extraction-v2`
要求逐字来源证据；用户偏好、用户事实和规则只能来自用户明确陈述，用户问题、助手复述与
已有正式记忆不能被重新提取为候选。

候选维护不依赖 Extractor。`MemoryCandidateMaintenanceWorker` 始终运行，使用 PostgreSQL
skip-locked 有界领取并自动终结到期候选。数据库 partial unique index 保证不同 Run 之间也
只能存在一条完全相同的 pending 候选；Repository/Application Service 的预检查用于正常幂等，
数据库约束负责并发兜底。
