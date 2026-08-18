# P2 可靠性与安全加固门禁

## 目标与边界

P2 对应 Roadmap Phase 8：把已经通过 MVP RC1 的 single-agent 系统，从“核心旅程可用”加固到
“可长期自用、可恢复、可审计、容量受控”。本阶段不扩展 Multi-Agent、桌面封装、插件市场或新的
RAG/Skill 产品能力。

所有演练遵守以下不变量：

- PostgreSQL 是 Task、Run、Step、ToolCall、Permission、AuditLog 和 Artifact metadata 的业务真源。
- Redis 只承担队列、命令、事件、heartbeat 和短期协调；禁止在承载用户任务的 Redis 上执行
  `FLUSHALL`。Redis 丢失演练必须使用专用实例或可验证的隔离环境。
- 故障恢复不得猜测未知工具结果、伪造成功、重复副作用或绕过 ToolGateway/PermissionManager。
- 备份恢复演练不得覆盖主数据库；只恢复到带随机后缀的临时数据库，验收后删除临时数据库，保留备份
  文件和脱敏摘要。
- 日志和证据只保存 ID、状态、计数、hash、稳定错误码和有界安全摘要，不复制 prompt、文件正文、
  API key、token、密码、cookie 或完整工具参数。

## P2 门禁分层

| 门禁 | 证明内容 | 运行条件 |
| --- | --- | --- |
| H0 静态与自动化 | 契约、状态机、重试/容量边界、脱敏、迁移链和测试质量 | 服务可关闭 |
| H1 数据恢复演练 | Alembic 单 head/current、备份可读、隔离恢复、关键表精确行数一致 | 只需 PostgreSQL |
| H2 进程故障注入 | Gateway、Agent Worker、RAG Worker、Redis 分别重启后恢复 | 专用本地运行环境 |
| H3 长任务/SSE | SSE 断开与重连、Gateway 重启、长任务终态和事件去重 | 完整 Runtime |
| H4 运行面对账 | pending/retry/DLQ/ACK 与 PostgreSQL Run/Event/Outbox/Inbox 终态一致 | 专用 Redis |
| H5 审计与容量 | 安全导出、保留策略、敏感字段脱敏、所有资源显式上限 | 自动化 + 数据库 |

任何门禁失败都必须保留失败证据并停止当前演练；不得以页面观感、历史日志或另一个 revision 的结果
替代。涉及副作用的任务必须写入 Git 忽略的 `tmp/p2-hardening/`，使用唯一文件名并检查至多执行一次。

## 故障注入矩阵

| 故障点 | 注入时机 | 必须恢复的状态 | 阻断条件 |
| --- | --- | --- | --- |
| Gateway | SSE 已建立且 Run 非终态 | HTTP health、同一 Run 历史、SSE 补偿、无重复 event id | Run 丢失、终态被 Gateway 猜测、重复事件 |
| Agent Worker | queued/running、waiting permission 或获批后的 effect 前屏障 | lease/reconciliation、checkpoint、安全重领、最终终态 | 重复工具副作用、永久 running、未知结果重放 |
| RAG Worker | parsing/embedding 中，含最后一次 attempt 的 lease 过期 | Job lease、attempt、Document 状态、Chunk/Vector 原子性 | ready 但无向量、重复 Chunk、无界重试、永久 indexing |
| Redis | pending permission 或已持久化历史存在 | consumer group、heartbeat、Outbox 重发、PostgreSQL 历史 | Redis 成为真源、历史丢失、重复执行副作用 |

每次故障只注入一个变量。重启前后记录 Task/Run/Step/ToolCall/Permission/RAG Job ID、状态、事件
序列、Outbox/Inbox 计数、Redis PEL/DLQ 计数和文件 hash。Gateway、Worker 与 Redis 演练至少覆盖一次
等待权限任务；RAG Worker 演练至少覆盖一次非终态 ingestion job。

REC-07 必须使用 ToolGateway 的真实 effect 前屏障，不得用模型提示、固定 sleep 或观察模糊日志猜测时机：

1. 在专用 Worker 环境设置 `JARVIS_TEST_FAULT_INJECTION_ENABLED=true`、绝对
   `JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT`，并发起需要 `allow_once` 的 `workspace.create_file`。
2. 批准后等待唯一 `*.reached.json`；此时 PostgreSQL 必须已有 `permission.resolved + tool_in_flight`，目标
   文件必须不存在。立即强杀 Agent Worker，不创建 release。
3. 关闭故障注入后重启 Worker；reconciliation 必须按 effect unknown 不可恢复失败收口，不能再次执行
   工具，目标文件仍不存在。保留 checkpoint、ToolCall、Permission、AuditLog 与 marker 作为证据。

REC-08 在最后一次 parsing/chunking attempt 中强杀 RAG Worker，等待 lease 到期后重启。若没有剩余预算，
Job 必须为 `failed/RAG_INGESTION_ATTEMPTS_EXHAUSTED`，Document 必须同步 `failed`，owner/lease/retry 清空且
存在 `rag.ingestion.failed` 审计；若仍有预算则允许新 owner 领取。两种路径都不得永久 processing、生成
重复 chunks/vectors 或伪装 ready。

## 长任务与 SSE 稳定性

- 在 Run 非终态时主动断开 SSE，再用同一 Run ID 重连；服务端必须先返回 PostgreSQL 历史，再补实时
  事件，event id 不重复、sequence 单调且只有一个终态。
- Gateway 重启不能修改 Task/Run 业务状态；重连后必须从 Control Plane/PostgreSQL 恢复。
- 等待权限、模型调用、RAG 入库三类长等待至少各覆盖一次；客户端断开不能取消任务。
- 测试必须设总超时、静默超时、最大重连次数和最大收集事件数，避免测试本身成为无界消费者。

## Migration、备份与恢复

- Alembic 必须只有一个 head，主库 `alembic current` 必须等于代码 head；发现多 head 或落后立即失败。
- 使用 PostgreSQL custom-format 备份并执行 `pg_restore --list` 可读性校验。
- 恢复目标只能是新建临时数据库；禁止 `--clean` 指向主库，禁止删除主数据库或 volume。
- 恢复后比较 Alembic revision、公共表集合和关键业务表精确行数。至少覆盖 Task/Run/Event/Step、
  ToolCall/Permission/AuditLog/Artifact、RAG Document/Job/Chunk/Embedding、Outbox/Inbox。
- 演练结果记录备份 SHA-256、字节数、代码 revision、数据库 revision、开始/结束时间和行数摘要；不记录
  敏感行内容。

当前演练入口：

```bash
scripts/p2-data-recovery-drill.sh
```

脚本会按需启动 PostgreSQL，验证 Alembic 单 head 与主库 current，生成 custom-format 备份并恢复到
`jarvis_p2_restore_*` 临时数据库，对比公共表集合和 14 张关键表精确行数。无论成功或失败都只删除
该前缀且经过校验的临时库；若 PostgreSQL 原本未运行，结束时会将其停止。备份与脱敏摘要保存在
`.local/p2-hardening/data-recovery-drill/<UTC timestamp>/`。

## Pending、Retry、DLQ 与终态对账

- 三条 Redis stream 分别检查 group、pending idle、delivery count、retry backoff、最大投递次数和 DLQ。
- poison message 必须原子 DLQ + ACK；可恢复错误必须留在 PEL；耗尽时先用 PostgreSQL RuntimeEvent 与
  AuditLog 收口，再进入 DLQ。
- 每个 terminal Run 必须只有一个 terminal RuntimeEvent；不得残留 running/pending Step 或 ToolCall；
  waiting permission 必须对应唯一 pending PermissionRequest。
- Outbox published/failed、Inbox processed、Redis ACK 和 PostgreSQL terminal state 需要同一快照报告。
- 人工重试继续固定为 L3，重新读取 PostgreSQL Task/Run/Workspace 创建 replacement Run，不重放 DLQ
  原 payload。

## 审计治理与敏感字段

- 导出只允许使用 AuditLog 安全投影和固定字段顺序；默认 JSONL，可选 CSV。必须分页流式处理并设置
  最大行数/最大字节数，不在内存中加载全表。
- 导出动作本身必须写 AuditLog，记录筛选条件摘要、行数、hash 和结果，不记录导出正文。
- 保留策略只处理超过显式保留期的审计记录；必须先 dry-run，再创建 L4 单次确认，按事务批次执行
  且有最大处理量。高风险权限决定、审计保留自身、恢复和数据删除类事件永久保护。禁止自动调度、
  长期授权和未经确认删除。
- Python、Go、Redis DLQ、RuntimeEvent、AuditLog、SSE 和导出分别运行敏感键、Bearer/token、URL
  credential、异常文本和嵌套 JSON 脱敏回归。

## 容量预算

P2 完成前，以下资源必须在代码中具有默认值、最小/最大配置边界、拒绝或外置策略，以及测试：

- Redis stream 长度、PEL stale 扫描批次、DLQ 长度/TTL、Outbox 发布批次。
- 单 Run 最大步骤/迭代/恢复/模型纠错次数、模型请求超时、工具超时、RAG Job 尝试次数。
- 会话历史、Memory、RAG Context、ToolResult、RuntimeEvent payload 和最终输出的字符/token 上限。
- Artifact 单对象、单 Run、单 Workspace 与本地总容量；RAG Asset 单对象和总容量。
- Audit 查询/导出页大小、导出最大行数/字节数、保留任务批次。

只有“配置存在”不算通过；负值、零、超大值和接近边界值都必须有回归测试，达到上限时返回稳定、
可观察、不可泄密的 `AppError` 或采用已记录的受控外置策略。

## 完成标准

P2 完成需要同一 revision 的 H0–H5 全部通过，并在 `docs/12-development-progress.md` 记录：

- 每个门禁的结果目录和摘要。
- 故障注入使用的安全环境、Task/Run/Job ID 和恢复结论。
- 备份 hash、临时恢复库名、对账表数量和是否已删除临时库。
- 审计导出/保留演练与脱敏测试结果。
- 容量预算表和仍未覆盖的风险。

在此之前只能表述为“P2 进行中”，不得以 Phase 8 已有的局部能力宣称整体完成。

## 当前执行状态（2026-07-31）

- H1 已在 migration head `018_outbox_redis_recovery` 通过；证据位于
  `.local/p2-hardening/data-recovery-drill/20260731T005614Z/`。
- H2/H3 第一轮无副作用 smoke 已通过 Gateway、Agent Worker、RAG Worker 与 Redis 分别重启，以及
  Gateway 重启后的同 Run SSE 历史恢复；证据位于
  `.local/p2-hardening/runtime-fault-drill/20260731T005422Z/`。
- Redis 演练已在真实运行中验证存活进程可重载丢失的 Lua script，并恢复 run-queue、runtime-event 与
  heartbeat consumer group；等待权限中的 Redis 重启与真实 permission command 已在
  `.local/p2-hardening/runtime-fault-drill/20260731T011811Z/` 通过，工具只执行一次。Outbox Redis
  传输重试保持默认最大 20 次的有限预算。
- H4 已取得 Redis/PostgreSQL/Storage 干净终态快照；2 条历史 dead Outbox 已脱敏归因为旧契约的
  `model.call.started/completed + UNKNOWN_EVENT_TYPE`，不自动重放。三条业务 stream 的 poison message
  与 Run Queue retry exhausted 真实接管路径已在
  `.local/p2-hardening/runtime-fault-drill/20260731T015418Z/` 一并通过。隔离 Run
  `98cc164d-9732-4bd9-91dd-54695c42f836` 的契约有效消息在三次 claim 失败后以
  `RUN_QUEUE_RETRY_EXHAUSTED` 原子 DLQ+ACK，Task/Run 同步 failed，且只有一个
  `agent.run.failed`；生产 65 秒最小退避未修改，演练只提升该 pending 消息的 idle。三类 poison
  message 仍为首次投递 DLQ，API 只保留 payload 大小与 SHA-256。
- 正式终态快照为 Redis 三条源 stream `pending=0 / lag=0`，DLQ 计数
  `run_queue=2 / worker_command=1 / runtime_event=1`；Storage 最近 20 个 Run
  `healthy / issue_count=0`；PostgreSQL active Outbox、processing Inbox、终态事件冲突、终态 Run
  未收口 Step/ToolCall 与 pending Permission 均为 0。H4 已通过。
- H5 第一批容量边界已落地：模型超时严格为 1–600 秒，模型输出 token 配置严格为
  1–131072，`AgentRunner.max_iterations` 严格为 1–20，共享 AgentAction parser 的最终回复严格不超过
  32,768 字符。负值、零、超上限和精确上边界针对性测试已通过。
- Audit 安全投影除敏感 key 外，现对所有字符串值统一清理 Bearer、`sk-*`、JWT、敏感
  `key=value` 和 HTTP(S) URL userinfo；嵌套 dict/list、`action_summary` 与 details 使用同一 owner，
  防止凭据藏在普通 note/message/endpoint 字段中穿透。
- Audit 安全导出已提供 JSONL/CSV 两种固定字段格式，以 100 条页面稳定读取；默认
  `5,000 rows / 5 MiB`，硬上限 `10,000 rows / 10 MiB`，Gateway 进行第二层字节限制并设置
  `no-store/nosniff`。导出完成或中断只审计筛选摘要、计数、SHA-256、预算和截断结果，不记录正文；
  CSV 同时阻止公式注入。
- Audit 保留策略预演已落地：普通 90 天、L3/权限类 365 天，L4/L5、删除/清理/撤销和恢复/
  修复/还原类永久保护；默认最多扫描 1,000 条并选出 100 条候选，硬上限分别为 10,000/1,000。
  预演只返回计数并写 `audit.retention.previewed`。
- Artifact 已实施单对象/Run/Workspace/本地总量四级配额，默认
  `50 MiB / 250 MiB / 2 GiB / 10 GiB`；RAG Asset 已实施默认 `16 MiB / 20 GiB` 的单对象与总量配额。
  两者通过同根目录跨进程锁串行检查与原子替换，目录统计最多扫描 100,000 个条目且不跟随 symlink。
  超限使用稳定、不含路径的错误码；最终回复采用有界内联降级，用户上传、文献下载与 RAG ingestion
  fail closed。配置负值、零、超上限、倒置关系、配额命中、覆盖不重复计量及扫描预算均有回归。
- 实际审计保留执行器已落地为 `audit.apply_retention_policy` L4 Always Confirm 两步入口：
  创建请求只冻结候选计数和 SHA-256，批准后在同一事务内锁定请求与全局执行器、按原时间点重新
  扫描分类，快照不一致即中止；一致时最多删除本批上限，权限消费、删除和永久结果审计原子提交。
  只接受 `allow_once / deny`，拒绝同样审计，禁止长期授权、自动调度和候选 ID/正文外泄。
- H2/H3 最终候选已在
  `.local/p2-hardening/runtime-fault-drill/20260731T025800Z/summary.json` 通过。源码状态为
  revision `32ccf35092c006428ba85faa1d4400b9006ecbb7` 加未提交工作区指纹
  `6a9f8a53e6bbbc2cbc273425daef19863ff90e1b83821d41547710a0571ab272`。模型调用已开始且 Run
  仍为 running 时断开 SSE，Gateway 重启后用 `Last-Event-ID` 续传 7 条未确认事件，完整 PostgreSQL
  历史为 9 条且唯一终态 completed。RAG Job 从 queued 进入 parsing 后对 Worker 进程组执行 SIGKILL；
  10 秒隔离 lease 到期后新进程接管，最终 Document ready、Job completed，22 个 Chunk 与 22 个
  Embedding 精确一致。等待权限期间 Redis 重启仍只执行一次工具，最终 Redis/Storage/PostgreSQL
  对账全部健康。H2/H3 已通过。
- 两次装置失败保留为证据且不计通过：`20260731T024540Z` 暴露任务未显式绑定 Workspace UUID；
  `20260731T024840Z` 暴露短文本 PDF 无可索引内容，安全错误为 `RAG_NO_INDEXABLE_CONTENT`。另一次
  `20260731T025525Z` 在 completed 后的单点快照产生瞬时读时序误判；只读复核为 ready/completed、
  22/22、未向量化 0，正式脚本改用最多 10 秒的有界一致性等待并在超时公开安全计数。
- H5 一次性临时 PostgreSQL 执行器演练已通过，证据位于
  `.local/p2-hardening/audit-retention-drill/20260731T082035Z/`；4 条候选精确删除，4 条延长/
  永久/近期记录全部保留，deny、重新申请、批准幂等和永久审计均通过，临时容器与数据卷已删除。
- H2/H3/H4 已在源码指纹
  `627362f69997b2fd37334c7019033a72e940a9fc78ef700d3bc383af71d59328` 下重跑通过，证据位于
  `.local/p2-hardening/runtime-fault-drill/20260731T082144Z/`；H1 同候选重跑证据位于
  `.local/p2-hardening/data-recovery-drill/20260731T082315Z/`。
- 最终 H0 完整 Python `1188 passed`、Gateway test/vet、Web `73 passed`/build、Shared typecheck、
  Ruff、compileall、脚本语法和差异检查全部通过。H0–H5 已齐备，当前整体状态为 **P2 已完成**。
  验收内容已冻结为 P2 收口提交，放行结论绑定验收前 Git base
  `32ccf35092c006428ba85faa1d4400b9006ecbb7` 与上述源码指纹；远端推送完成前不得混入新的未验收
  源码改动。
