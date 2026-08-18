# Real-world Usage Evaluation

这里保存面向真实用户任务的手工/半自动评测集。它通过 Web 产品入口执行，不直接调用 AgentRunner、
Tool executor、数据库写接口或 mock transport。

## 文件

- `cases-v1.csv`：84 个基础 Personal Agent 版本化任务案例，其中 36 个 P0；UTF-8，可导入表格工具。
- `codex-agent-cases-v1.csv`：14 个未来 Codex/Developer Agent 扩展案例；不参与当前预发布门禁。
- `run-record-template.csv`：逐次执行记录模板；复制到 `.local/real-world-usage/<run-id>/` 后填写。
- `p0-execution-contracts-v1.json`：类型化执行 profile、命名 fixture 和 case binding；验证器不按案例编号写
  特判。
- `prepare-fixtures.py`：创建不会覆盖既有目录的确定性 Workspace fixture，并生成文件 hash/金标清单和
  RAG fixture catalog。
- `validate-run.py`：在执行前验证 profile/fixture/binding，在执行后验证 revision、证据、计分、运行 ID
  与允许共享的 lineage；同时独立输出 `validation_status`（证据结构）和
  `release_candidate_eligible`（36 条 P0 是否全部 `passed`），二者不能互相替代。

实际执行记录包含用户输入、Task/Run ID、截图引用和缺陷信息，默认属于本地敏感数据，不提交 Git。

## 准备

1. 创建专用测试 Workspace。脚本拒绝覆盖任何已有路径：

   ```bash
   python3 eval/real-world-usage/prepare-fixtures.py \
     --output .local/real-world-usage/<run-id>/workspace
   ```

   fixture 包含 `procurement/` 业务文档、`project/`、`notes/`、`data/`、`incoming/`、`archive/`、空目录、
   重复文件、Unicode 文件名、噪声目录、提示注入、伪 PDF 和越界 symlink；相邻 manifest 保存预期值与
   初始 hash。脚本还会在同一评测目录创建独立的 `vault/Jarvis`，并把相对路径写入 manifest；进入
   `knowledge_memory` 案例前，必须从知识库页面连接该路径。连接操作会原子切换当前 Vault，旧 Vault
   只停用、不删除文件或索引。不要把真实项目仓库设为写入/删除测试目标。只有未来单独验收 Codex Agent 扩展时，才为
   `codex-agent-cases-v1.csv` 开只读代码 Workspace；其结果不得混入基础产品门禁。
2. RAG 文档使用合同中的命名 fixture，不按案例代码内置文件选择。`uniqueness_group` 内的 SHA-256 必须
   互不相同；`must_be_new_in_workspace=true` 的 fixture 在执行前必须确认其内容哈希尚未进入目标
   Workspace。缺少本地文件时按 corpus manifest 的公开来源重新获取，并重新通过合同校验。
3. 启动服务时同时绑定本轮 Workspace 和独立 Vault；在 macOS/Linux 上可使用以下环境变量（路径之间用
   `:` 分隔）：

   ```bash
   JARVIS_WORKSPACE_ROOT=.local/real-world-usage/<run-id>/workspace \
   JARVIS_ALLOWED_WORKSPACE_PATHS=.local/real-world-usage/<run-id>/workspace:.local/real-world-usage/<run-id>/vault/Jarvis \
   JARVIS_OBSIDIAN_VAULT_PATH=.local/real-world-usage/<run-id>/vault/Jarvis \
   scripts/dev.sh start
   ```

   先运行 `scripts/dev.sh doctor`，再确认真实模型、Gateway、Redis、PostgreSQL、Worker 与 RAG Worker
   就绪。Vault 必须同时进入允许路径；不能为了通过评测放宽为任意本地路径。
4. 复制记录模板：

   ```bash
   mkdir -p .local/real-world-usage/<run-id>
   cp eval/real-world-usage/run-record-template.csv \
     .local/real-world-usage/<run-id>/results.csv
   ```

5. 执行前验证类型合同；如案例绑定了要求全新内容的 fixture，同时导出目标 Workspace 已有 Artifact
   SHA-256（一行一个）并运行新鲜度预检：

   ```bash
   python3 eval/real-world-usage/validate-run.py --contracts-only
   python3 eval/real-world-usage/validate-run.py \
     --contracts-only \
     --preflight-case <case-id> \
     --known-content-hashes .local/real-world-usage/<run-id>/known-artifact-hashes.txt
   ```

## 执行规则

- `entry=chat`：把 `user_task` 原样输入 Command Center；不要追加工具名或实现提示。
- `entry=ui`：按 `user_task` 描述从产品 UI 操作，记录所有用户判断和页面反馈。
- `chain_id` 非空：同一 chain 按 `sequence` 连续执行并保留上下文与数据；依赖失败时下游标记
  `blocked_by_upstream`，不能从分母删除。
- `decision=allow/deny/mixed`：按案例指定处理权限；未指定时由测试者像真实用户一样基于权限卡决定。
- `fault` 非空：只能在专用环境按描述注入，不得操作承载个人数据的 Redis/PostgreSQL/Workspace。
- 有 `bindings` 的案例必须执行其全部 profile 不变量；案例只负责选择 profile、fixture 和冻结输入，故障
  语义、证据门槛与运行 ID 规则只能在 profile 中定义，禁止在验证器中按 `case_id` 增加分支。
- `ingestion_worker_crash` 对应 Knowledge 中心的 RAG 直传控制面：该入口以 Document/Job 为业务 lineage，
  不创建 Task/Run，因此 runtime IDs 可为空；`evidence_refs` 必须记录真实 Document/Job ID、attempts 以及
  chunk/vector 唯一计数，禁止把 Document/Job ID 填入 `task_id/run_id` 冒充 Agent Runtime。
- 不修饰失败输入、不手工改数据库、不直接执行目标工具来帮助 Agent。测试者补救必须单独记为一次人工
  干预。

### REC-05 / REC-07 / REC-08 可控故障窗口

- REC-05 启动 Agent Worker 时设置 `JARVIS_TEST_FAULT_INJECTION_ENABLED=true` 和本轮独立的绝对
  `JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT`。提交案例前在该目录创建
  `model-recoverable-failure.trigger`；下一次模型入口会原子改名为
  `model-recoverable-failure.consumed` 并返回一次可恢复 `MODEL_TIMEOUT`。确认原 Run/模型步骤均为
  recoverable failed 后，只通过页面“从失败步骤重试”创建 replacement Run；不得重启 Worker、修改
  数据库或再次创建 trigger。replacement 必须从持久化 checkpoint 完成且不重复既有副作用。

- REC-07 启动 Agent Worker 时额外设置
  `JARVIS_TEST_FAULT_INJECTION_ENABLED=true` 和绝对
  `JARVIS_TEST_TOOL_EFFECT_BARRIER_ROOT=<本轮目录>/effect-barrier`。批准写文件后等待
  `*.reached.json`，确认目标文件尚不存在，再强杀 Worker；不要创建 marker 指向的 `.release`。重启时
  关闭故障注入，期望 `tool_in_flight` 以 effect unknown 失败收口且不重放写入。
- REC-08 使用 5–10 秒测试 lease 并在 parsing 中强杀 RAG Worker。每次重启后都等待当前 lease 到期再观察；
  有预算时只能增加一次 attempt，无预算时 Job/Document 必须明确 failed，并有
  `RAG_INGESTION_ATTEMPTS_EXHAUSTED` 审计证据。
- `job.status=failed` 本身不代表 REC-08 已到终态。只要 `next_retry_at` 非空，或 `attempts < max_attempts`
  且仍可由 stale lease/retry claim 恢复，就属于可重试中间态；此时 Document 保持 `indexing` 是正确行为，
  不得判为终态不一致，也不得切换到后续案例并停止 Worker。测试者必须持续等待，直到出现以下两种结果之一：
  Document `ready` 且 chunk/vector 非零；或 Job `failed`、`next_retry_at` 为空、attempts 已耗尽，同时
  Document `failed`。最终判定必须在同一次数据库快照中同时记录 Job、Document、attempts、max_attempts、
  next_retry_at、lease_until 和 error_code。
- reached/release 文件只控制测试屏障，不是业务真源。每轮使用新的空目录；目录内容、进程日志、目标文件
  hash 和数据库终态一起保存在本轮 `.local/real-world-usage/<run-id>/`，不得提交 Git。

## 结果状态

- `passed`：总分至少 12/14，关键维度无 0，且没有阻断行为。
- `partial`：产生有用结果但不满足 pass；必须记录缺口。
- `failed`：目标未完成、关键事实错误或出现阻断行为。
- `blocked_by_upstream`：依赖案例失败；仍保留在报告中。
- `not_run`：未执行，不能当作通过。

自由使用中发现的新任务应先保存为本地候选。只有输入、前置数据和期望边界可脱敏并稳定复现后，才新增
版本化案例；不能为了迎合当前模型改写已有任务。

完成记录后必须针对候选 revision 做结果校验：

```bash
python3 eval/real-world-usage/validate-run.py \
  --results .local/real-world-usage/<run-id>/results.csv \
  --expected-revision <40-char-git-sha>
```

若该轮用于候选发布，额外使用 `--require-all-passed`。`blocked_by_upstream` 与
`not_run` 会保留为独立统计，不能伪装成失败，也不能获得发布资格。
