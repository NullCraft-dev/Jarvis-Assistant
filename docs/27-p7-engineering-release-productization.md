# P7 工程化与发布产品化

## 目标与边界

P7 把已经完成 P2 可靠性加固、P3 产品体验、P4/P5 RAG 质量闭环和 P6 Runtime 框架收口的系统，推进到
可重复验证、可持续集成、可安全升级和可长期运维的 RC2 工程基线。

本阶段不进入桌面封装、Multi-Agent、插件市场，也不在缺少质量台账证据时修改 BM25、Query Rewrite、
向量策略或模型。发布门禁只验证既有产品能力，不拥有 Runtime、RAG、权限或持久化业务真相。

## 固定顺序

1. P7-1：RC2 发布基线、结构化门禁报告与 CI。
2. P7-2：首次启动、配置校验与依赖自检。
3. P7-3：migration、备份、恢复与升级操作产品化。
4. P7-4：运行诊断、脱敏支持包与故障定位入口。
5. P7-5：同一 revision 的 RC2 候选验收与发布记录。

## P7-1：RC2 发布基线与自动化门禁

### 门禁分层

| 层级 | 入口 | 证明内容 | 运行位置 |
| --- | --- | --- | --- |
| E0 确定性代码门 | `scripts/release-gate.sh ci` | Shared、Gateway、Web、Worker 的类型、测试、构建、静态质量和报告生成器自检 | GitHub Actions / 本地 |
| E1 Runtime smoke | `scripts/release-gate.sh runtime` | Gateway、PostgreSQL、Redis、有效 Worker、Task 与 SSE 基础链路 | 已启动的本地 Runtime |
| E2 RAG 发布门 | `scripts/release-gate.sh rag` | 固定 promoted cohort 在当前生产 Pipeline 的重放和版本基线 | 显式数据库连接的本地候选环境 |
| RC2 工程门 | `scripts/release-gate.sh rc2` | 干净候选上的 E0 + E1 + E2 | 本地 release candidate 环境 |

`automated` 继续作为 `ci` 的兼容别名；`p4`、`evidence` 和 `rc1` 继续保留原语义。RC2 不复用 RC1
evidence 冒充新候选证据，也不在共享 CI runner 上访问个人 PostgreSQL、模型密钥或本地 Artifact。

### RC2 本地执行

完整 Runtime 必须已启动，工作区必须干净，并显式提供数据库连接：

```bash
JARVIS_DATABASE_URL='postgresql+asyncpg://...' scripts/release-gate.sh rc2
```

`rc2` 串行执行确定性代码门、Runtime smoke 和 promoted-only RAG 门；任一步失败立即阻断。P2 的隔离
故障注入、备份恢复和审计保留演练仍是 P7-5 候选验收证据，不会在每次普通 CI 中自动操作 Docker、
重启本地服务或创建临时恢复库。P7-3 会把这些安全演练整合为明确的升级/恢复操作入口。

### 结构化报告契约

每次门禁都写入 Git 忽略的 `.local/release-gate/<UTC timestamp>/`：

- `summary.txt`：供人快速阅读的 key/value 总结。
- `steps.tsv`：逐步骤状态、退出码、耗时和相对日志文件名。
- `report.json`：供 CI、后续质量中心或发布工具读取的机器报告。
- `<step>.log`：该步骤的完整本地输出。

`report.json` 固定包含：

```json
{
  "schema_version": "1.0",
  "gate_id": "jarvis-release-gate",
  "mode": "ci",
  "status": "passed",
  "revision": "<git sha>",
  "worktree_clean": true,
  "release_candidate_eligible": false,
  "started_at": "<UTC ISO datetime>",
  "finished_at": "<UTC ISO datetime>",
  "passed_steps": 15,
  "failed_steps": [],
  "steps": []
}
```

只有干净工作区中通过的 `rc1` 或 `rc2` 报告才能把 `release_candidate_eligible` 标为 `true`。普通
`ci/runtime/rag` 通过只代表对应层级通过，不能单独宣称 RC2 已放行。

报告只保存 revision、状态、计数、耗时和相对日志索引，不复制 prompt、模型输出、环境变量、数据库
URL、API key、token、Cookie 或工具参数。业务真相继续属于 PostgreSQL、AuditLog 和 RuntimeEvent。

### GitHub CI

`.github/workflows/rc2-ci.yml` 在 `main` push 和 pull request 上运行 E0：

```text
checkout
-> Go / Node / Conda 环境
-> 锁文件安装 Web 与 Shared 依赖
-> 安装 Agent Worker dev 依赖
-> scripts/release-gate.sh ci
-> always 上传脱敏门禁目录
```

Workflow 使用只读仓库权限、45 分钟总超时和同分支并发取消。CI 不注入模型或数据库 secret，也不执行
真实 Runtime/RAG/故障注入，因此不能替代本地 `rc2`。

## P7-1 完成标准

- `ci` 与旧 `automated` 使用同一确定性实现，不形成两套测试真源。
- `rc2` 要求干净工作区并串行执行 E0/E1/E2。
- 成功与失败均生成可校验 JSON；步骤失败保留非零退出码和失败步骤。
- GitHub Actions 只消费统一脚本，不复制各模块测试命令。
- CI 和报告不泄漏 secret，不把日志或本地产物提交到 Git。
- 脚本自检、Shell 语法、Python Ruff、Workflow YAML、真实 Runtime 报告与完整 E0 门禁通过。

## P7-2：首次启动、配置校验与依赖自检

### 唯一入口与 owner

首次启动前继续使用既有入口：

```bash
scripts/dev.sh doctor
```

`doctor` 先调用无第三方依赖的 `scripts/dev-preflight.py` 生成结构化报告，再执行原有 Shell 启动检查。
系统命令、项目清单、Node 依赖、目录和端口由 preflight owner 检查；模型、容量与 RAG 配置不在脚本中
复制规则，而是通过 Conda Runtime 调用生产 `WorkerConfig`、Provider config 和 `RagWorkerConfig`。

### 检查分级

| 状态 | 含义 | 启动行为 |
| --- | --- | --- |
| `passed` | 必需能力或显式配置已就绪 | 继续 |
| `warning` | auto 模式的可选能力未安装，或显式跳过某项诊断 | 允许继续，并展示降级与修复建议 |
| `failed` | 缺少必需依赖、生产配置非法、目录越界、端口冲突或显式要求的能力不可用 | 阻断启动 |

总状态相应为 `ready / degraded / blocked`。`scripts/dev.sh start` 必须先通过同一 doctor；不得在 preflight
失败后继续启动一部分服务，形成难以解释的半可用状态。

### 检查范围

- Docker、Conda、Go、npm、curl、Docker Compose 与 Docker daemon。
- 受版本控制的 compose、Python、Go、Web、Shared 清单。
- Conda 环境和生产 Runtime imports。
- Web/Shared 锁文件依赖完整性与 Gateway Go module 完整性。
- `.env` 存在性和 `0600` 权限；外部环境变量仍优先。
- 生产模型 Provider、Base URL、模型名、命名密钥变量和 RAG Embedding 配置。
- 默认 Workspace 的绝对路径、读写权限，以及 1-32 个允许根目录对默认 Workspace 的覆盖。
- Artifact / RAG Asset 目录能否由现有父目录安全创建。
- MLX-VLM / BGE Reranker 的 `auto / true / false` 语义。
- Control Plane、Gateway、Web 以及已启用 MLX-VLM/Reranker 的端口是否可绑定。

### 报告与敏感信息边界

报告默认写入 `.local/preflight/<UTC timestamp>/report.json`，可用
`JARVIS_PREFLIGHT_OUTPUT_DIR` 改变根目录。该目录进入 Git ignore。

报告只包含稳定 check id、category、状态、阻断性、安全摘要和修复建议。它不包含环境变量值、密钥名、
API key、数据库 URL、Workspace 绝对路径、模型响应或 subprocess stderr。生产配置校验失败统一映射为
`config.runtime`，具体敏感值只留在受控本机配置中。

### P7-2 完成标准

- `doctor` 和 `start` 复用同一 preflight，不形成仅供测试的旁路。
- 有效本机环境得到 `ready`，随后完整服务可以启动并通过健康检查。
- 缺少命名密钥时 `config.runtime` 失败关闭，报告不出现密钥名或值。
- 已运行服务造成的端口冲突能准确阻断并给出端口级修复建议。
- 可选 Runtime 在 auto 缺失时只 warning，显式 true 缺失时必须 failed。
- 新建 `.env` 权限为 `0600`；历史权限过宽时 doctor 明确提醒。
- preflight self-test、Ruff、Shell 语法、完整 E0 门禁和真实 `doctor -> start` 通过。

## P7-3：migration、备份、恢复与升级操作产品化

### 统一入口

本地 Compose PostgreSQL 的数据生命周期由 `scripts/data-lifecycle.py` 统一管理：

```bash
scripts/data-lifecycle.py status
scripts/data-lifecycle.py backup
scripts/data-lifecycle.py restore-drill
scripts/data-lifecycle.py upgrade --confirm
```

`status` 只读核对唯一 Alembic code head 与数据库 current；`backup` 可在应用运行时创建 PostgreSQL custom
format 一致性备份并验证 catalog；`restore-drill` 在隔离临时数据库恢复刚创建的备份；`upgrade` 严格执行
备份、隔离恢复、migration 和升级后 revision 对账。

旧 `p2-data-recovery-drill.sh` 只保留历史 P2 证据兼容，不再是发布或日常升级入口。Alembic migration
继续是 schema 唯一真源，PostgreSQL 继续是业务真源，data lifecycle CLI 只负责操作编排与证据。

### 安全边界

- `restore-drill` 和 `upgrade` 检查 8100、8080、5173，应用仍运行时失败关闭，避免对账期间继续写入。
- `upgrade` 必须显式提供 `--confirm`；不提供时不创建备份、不执行 migration。
- 升级前必须完成 custom-format 备份、`pg_restore --list`、隔离恢复、migration revision、全部 public 表
  集合及逐表精确行数对账；任一步失败均不执行 migration。
- 临时数据库名使用受限前缀和安全标识符，禁止等于源数据库；演练结束无论成功失败都尝试删除临时库。
- `dev.sh start` 不再隐式执行 `alembic upgrade head`。数据库落后时启动阻断，并提示先走显式安全升级。
- 空数据库用稳定 revision `base` 表达，可经同一备份/隔离恢复链路初始化到 head，不另设旁路。
- 备份文件和 `report.json` 权限固定为 `0600`，运行目录为 `0700`，默认位于 Git 忽略的
  `.local/data-lifecycle/<UTC timestamp>/`。

### 结构化报告

每个操作都会写 `report.json`，包含 operation、status、Git revision、code/database head、备份 basename、
字节数、SHA-256、catalog 校验和恢复对账布尔值。失败只保存稳定 error code 与安全文案，不保存数据库
URL、密码、容器 stderr、临时数据库名或本地绝对路径。

### P7-3 完成标准

- status、backup、restore-drill、upgrade 共用同一实现和报告契约。
- status 能阻断数据库落后或 Alembic 多 head；backup 可独立恢复且权限收紧。
- 隔离恢复覆盖全部 public 表，revision、表集合和逐表行数完全一致，临时数据库已删除。
- 在线 restore/upgrade、未确认 upgrade、非法数据库标识符均失败关闭。
- 当前 head 上执行安全 no-op upgrade 后 database head 仍精确等于 code head。
- `dev.sh start` 只验证 current，不再静默修改 schema。
- self-test、Ruff、Python/Shell 语法和完整 E0 门禁通过。

## P7-4：运行诊断、脱敏支持包与故障定位入口

### 统一入口

```bash
scripts/runtime-support.py check
scripts/runtime-support.py bundle
```

`check` 生成本地结构化诊断目录；`bundle` 在相同诊断基础上生成 `0600` 的 tar.gz 支持包。两者读取既有
Gateway Runtime Health、Worker heartbeat 和 PostgreSQL reconciliation 安全投影，不新增旁路接口，也不
直接读取 Redis payload 或业务表。

Gateway 不可达时仍生成 `degraded` 本地支持包，保留容量、日志聚合和最近操作证据；诊断工具不会因为待
诊断服务停止而失去作用。Gateway URL 只允许本机 HTTP `/api` 地址，拒绝远端 host、userinfo、query 和
fragment，避免诊断入口成为网络访问旁路。

### 支持包白名单

归档成员固定为：

- `report.json`：健康/降级结论、稳定检查 ID 与安全修复建议。
- `health.json`：Gateway、Redis stream/DLQ 计数、Worker 聚合和 Storage 对账计数。
- `environment.json`：OS/架构/工具版本、Git revision 与 dirty 布尔值。
- `log-summary.json`：容量统计，以及按服务汇总的日志级别、字节数和 WARN/ERROR 调用位置。
- `operations-summary.json`：最近 release gate、preflight、data lifecycle 的白名单状态。
- `manifest.json`：成员 basename、字节数、SHA-256 和明确排除项。

支持包不包含原始日志或日志文件名、AuditLog、数据库 dump、Artifact、RAG 文档、`.env`、Workspace 路径、
task/run ID、prompt、模型输出、工具参数、API key、数据库 URL 或命令 stderr。诊断 API 单响应最多 1 MiB；
每个日志段最多扫描末尾 5 MiB、最多 50 个文件；容量遍历最多 100,000 个文件。所有 JSON 文件为 `0600`，
目录为 `0700`，默认写入 Git 忽略的 `.local/support-bundles/<UTC timestamp>/`。

### 状态与定位

- `healthy`：所有可用诊断项通过。
- `degraded`：服务不可达、Worker stale、Storage 对账异常、容量不足、日志不可用或 Runtime 有 warning/DLQ。
- 历史 DLQ 不自动删除，也不把支持包当作业务修复依据；仍须回到 Runtime Health，按 PostgreSQL 真源核对。
- 原始日志仍只留在本机 `.local/logs/`。支持包的 failure location 聚合用于确定服务与代码 owner，不承载
  用户输入或错误原文。

### P7-4 完成标准

- check 与 bundle 共用同一采集和白名单投影。
- 健康 Runtime 与 Gateway 不可达两种场景均能生成有界报告。
- 包内成员精确匹配白名单，manifest hash/size 可校验，文件权限收紧。
- 注入 secret 和自由文本不会进入日志摘要；真实归档扫描不出现密钥、数据库 URL 或本地绝对路径。
- 只输出聚合诊断，不越过 Runtime Health/Storage owner，不执行任何修复或外部发送。
- self-test、Ruff、Python 语法和完整 E0 门禁通过。

## P7-5：同一 revision 的 RC2 候选验收与发布记录

### 候选记录入口

最终 RC2 记录由 `scripts/rc2-candidate.py` 生成。它不运行测试或修复数据，只验证五类已经完成的本地证据：

```bash
scripts/rc2-candidate.py \
  --gate-report .local/release-gate/<stamp>/report.json \
  --support-dir .local/support-bundles/<stamp> \
  --data-report .local/data-lifecycle/<stamp>/report.json \
  --runtime-fault-report .local/p2-hardening/runtime-fault-drill/<stamp>/summary.json \
  --audit-report .local/p2-hardening/audit-retention-drill/<stamp>/summary.json
```

普通 RC2 流程固定为：提交候选源码并确保工作区干净；运行 P2 Runtime 故障注入、P7 数据隔离恢复和审计
保留演练；启动服务；执行 `release-gate.sh rc2`；生成 `runtime-support.py bundle`；最后创建候选记录。

### 同一 revision 约束

以下 revision 必须全部等于当前 Git HEAD：

- RC2 gate 的顶层 `revision`，且 `worktree_clean=true`、`release_candidate_eligible=true`。
- support bundle 的 `environment.git.revision`，且 `worktree_dirty=false`。
- data lifecycle 的 `revision`，并完成隔离恢复、临时库清理、revision/表集合/逐表行数对账。
- runtime fault drill 的 `revision`，且九个固定故障场景全部通过、工作区干净。
- audit retention drill 的 `source.revision`，且数据库安全检查、权限拒绝/单次批准、实际清理和脱敏通过。

任何旧 revision 证据、dirty 工作区、缺失场景或字符串化“passed”都不能生成候选记录。支持包可以因历史
`runtime.dead_letters` 为 degraded；其他 warning 均阻断 RC2 记录。

### 候选记录与边界

输出位于 `.local/release-candidates/RC2-<UTC>-<short sha>/`：

- `record.json`：revision、各门禁安全聚合、RAG 指标、恢复表数、场景数、审计计数和证据 SHA-256。
- `record.sha256`：`record.json` 的 detached digest。

目录为 `0700`、文件为 `0600`，并进入 Git ignore。记录不复制 task/run ID、prompt、模型输出、工具参数、
原始日志、AuditLog 正文、数据库 URL、secret 或本地绝对路径。它是本地候选证据索引，不是签名、发布 tag
或外部上传动作；创建 tag、GitHub Release 或分发包仍需用户另行授权。

### P7-5 完成标准

- 当前干净 revision 的 RC2 gate 包含 E0、Runtime smoke 和 promoted RAG，全部通过并标记 eligible。
- promoted cohort 不少于 10 条且所有质量检查通过。
- support、data recovery、runtime fault、audit retention 与 gate 全部绑定同一 revision。
- 候选记录只保存安全聚合和证据 hash，detached digest 可复核。
- dirty、revision mismatch、恢复不完整、P2 场景缺失、非允许 support warning 均失败关闭。
- self-test、Ruff、Python 语法和完整 E0 门禁通过。
