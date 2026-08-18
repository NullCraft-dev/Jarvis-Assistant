# MVP RC1 Release Gate

## 目标

RC1 Release Gate 用于回答一个问题：当前版本是否已经达到可长期自用的第一个发布候选，而不只是某个
模块或某次演示成功。

RC1 不增加产品能力。它把现有 single-agent 主链路固定为可重复执行的发布标准，并要求代码质量、运行
连通性和真实用户旅程三层证据同时通过。任一层失败，RC1 均不得标记为通过。

## 三层门禁

| 门禁 | 负责证明 | 统一入口 | 是否阻断 RC1 |
| --- | --- | --- | --- |
| G0 自动化质量门 | 共享契约、Go、Web、Python 的测试、类型、构建和静态质量 | `scripts/release-gate.sh automated` | 是 |
| G1 Runtime smoke | Gateway health、有效 Worker、Task 创建和 SSE 基础事件链路 | `scripts/release-gate.sh runtime` | 是 |
| G2 真实旅程证据 | 八条核心用户旅程及跨层业务证据 | `scripts/release-gate.sh evidence <file>` | 是 |

最终放行命令：

```bash
scripts/release-gate.sh rc1 /absolute/path/to/rc1-evidence.json
```

执行日志写入 Git 忽略的 `.local/release-gate/<UTC timestamp>/`。日志只记录测试输出，不应写入 API key、
模型原始敏感内容或未脱敏工具参数。

## G0 自动化质量门

统一入口依次执行：

1. RC1 evidence validator 自检。
2. `git diff --check`。
3. Shared TypeScript contract typecheck。
4. Gateway `go test ./...` 与 `go vet ./...`。
5. Web 全量 Vitest 与生产构建。
6. Agent Worker 全量 pytest、Ruff 质量门与 compileall。

任何命令非零退出即停止，不允许以历史测试记录替代当前 revision 的执行结果。

## G1 Runtime smoke

Runtime smoke 复用 `scripts/dev-runtime-check.sh`，验证：

```text
Gateway health
-> 至少一个未过期 Worker heartbeat
-> 创建 Task/Run
-> SSE 中出现 task.created
-> 默认要求 Run completed
```

它只证明系统已经连通，不证明权限、副作用、恢复或 RAG 产品链路正确。不得用 G1 替代 G2。

## G2 八条核心用户旅程

所有旅程必须在同一候选 revision、同一套 production-like 本地环境执行。使用真实 Gateway、Redis、
PostgreSQL、Agent Worker、真实模型和 Web 页面；mock、直接调用 executor 或手工写数据库不计入 G2。

| ID | 用户旅程 | 必须验证的关键结果 |
| --- | --- | --- |
| `conversation_no_tool` | 普通问答，不调用工具 | 有最终回复；ToolCall、Permission 为零；只允许 Runtime 最终回复 Artifact，不得产生工具交付物 |
| `workspace_read` | 从 active Workspace 读取文件并回答 | 只经过 L0 ToolGateway；读取范围和审计正确 |
| `workspace_create_allow_deny` | 两个独立任务分别批准和拒绝 L2 文件创建 | 批准只创建目标文件；拒绝不产生文件；两条决定均可审计 |
| `rag_ingest_retrieve` | PDF 下载/上传、入库、等待完成并指定文档检索 | Document ready、Job completed、Chunk/向量非零、回答只引用目标文档 |
| `rag_to_knowledge` | RAG 检索后写入个人知识库 | 独立 L2 批准；Markdown 与可信 provenance 同时持久化 |
| `pause_resume_cancel_retry` | 暂停/恢复、取消、可恢复模型失败重试 | 状态转换可见；未知工具结果不重放；replacement Run 可追踪 |
| `service_restart_recovery` | 长任务或等待权限期间重启 Gateway/Worker | 页面、Run、Permission、Timeline 从 PostgreSQL 恢复且不重复副作用 |
| `redis_state_loss_recovery` | Redis 短期状态丢失后的业务恢复 | PostgreSQL 保持真源；历史可恢复；系统不伪造成功或重复执行副作用 |

最后两条属于故障注入，只能在专用验收环境执行。Redis 丢失测试应通过重建专用 Redis 实例制造，不得在
承载用户其他任务的实例执行 `FLUSHALL`、删除 PostgreSQL 数据卷或清理真实 Artifact/知识库。

## 跨层证据要求

每条旅程必须对以下类别标记 `verified` 或 `not_applicable`：

- `user_visible_result`：页面截图、最终回复或文件结果。
- `task_run`：Task ID、Run ID 和终态。
- `steps`：关键 ExecutionStep ID 与顺序。
- `tools`：ToolCall ID、工具名、风险和结果。
- `permissions`：PermissionRequest ID、决定和范围。
- `audit_logs`：AuditLog ID 与事件类型。
- `events`：关键 RuntimeEvent ID/类型序列。
- `artifacts`：Artifact、RAG Document、Knowledge Document 或文件 hash。

`verified` 必须提供至少一个 `refs`；`not_applicable` 必须说明为何该旅程不产生该类记录。关键类别由
validator 按旅程强制要求，不能以 `not_applicable` 绕过。

建议引用格式：

```text
task:<uuid>
run:<uuid>
step:<uuid>
tool_call:<uuid>
permission:<uuid>
audit:<uuid>
event:<event_id>
artifact:<uuid>
rag_document:<uuid>
screenshot:<safe-relative-path>
```

## 证据文件

生成草稿：

```bash
python3 scripts/validate-rc1-evidence.py \
  --write-template .local/release-gate/rc1-evidence.json
```

草稿允许 `pending`，可在执行过程中检查结构：

```bash
python3 scripts/validate-rc1-evidence.py \
  --allow-pending .local/release-gate/rc1-evidence.json
```

正式门禁只接受八条全部 `passed` 且必需证据完整的文件：

```bash
scripts/release-gate.sh evidence .local/release-gate/rc1-evidence.json
```

证据文件只保存 ID、状态、安全摘要和本地截图引用；不得复制正文、密钥、token、完整工具参数或敏感
Memory。Task/Run/Permission/AuditLog 的业务真相仍在 PostgreSQL，证据文件只是发布验收索引。

## 阻断标准

以下任一情况直接阻断 RC1：

- G0 或 G1 任一步失败。
- 八条旅程未全部通过，或来自不同 revision。
- 页面成功但 PostgreSQL/RuntimeEvent/AuditLog 不一致。
- 副作用绕过 ToolGateway、PermissionManager 或 AuditLog。
- L2 以上动作没有正确确认，拒绝后仍产生副作用。
- 刷新或服务重启后状态丢失、重复执行工具或出现重复终态。
- Redis 被当成业务真源，短期状态丢失导致已持久化历史不可恢复。
- 错误或证据中泄漏敏感信息。

非阻断但必须记录的内容，仅限不影响上述八条旅程的明确后置能力，例如 Multi-Agent、桌面封装、MCP
HTTP/SSE、插件市场和 Memory 向量检索。

## RC1 结论规则

只有干净工作区中的 `scripts/release-gate.sh rc1 <evidence>` 对同一 revision 成功退出，证据文件的
`revision` 与当前完整 Git commit 完全一致，且结果目录中的 summary 标记 `status=passed`，才可以在
`docs/12-development-progress.md` 将 RC1 记为通过。在真实旅程尚未执行完之前，只能表述为
“Release Gate 已建立”或“G0/G1 已通过”，不能表述为“RC1 已完成”。
