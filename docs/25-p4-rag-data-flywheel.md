# P4-3 RAG 数据飞轮闭环

## 结论

状态：**自动链路、首版固定回归集和当前 Pipeline 重放门禁均已通过（2026-08-02）**。

P4-3 没有新建第二套评测系统，而是把既有
`rag_evaluation_traces -> privacy review -> human label -> promotion -> eval/framework`
接入单一自动入口。生产 RAG 每次真实检索仍自动采集 trace；后续候选挖掘、脱敏审核队列、指标计算、
版本对比和发布门禁由脚本自动完成。

## 自动与人工边界

```text
真实 rag.search
-> 自动采集候选、重排、Context 和版本 trace
-> 自动识别空结果、截断、Reranker 降级和分阶段失败
-> 自动生成不含正文的优先审核队列
-> 人工批准隐私
-> 人工确认正例、难负例和失败原因
-> 人工决定是否晋升为固定回归样本
-> 自动用当前生产 Pipeline 重放固定 cohort
-> 自动计算指标、比较基线并执行发布门禁
```

自动化不得执行三件事：批准隐私、把模型引用直接当成正确证据、将样本晋升为发布金标。这三个动作会
改变质量真相，必须由人确认。自动报告不包含原始 query、回答、Chunk 正文或 Embedding vector。

## 发布门禁契约

- 观察集：`confirmed + promoted`，用于长期趋势、失败挖掘和晋升候选排序。
- 发布集：仅 `promoted`，并由版本化 `rag-promoted-p4-v1` manifest 固定分母。
- 门禁策略：`apps/agent-worker/eval/tasks/rag-flywheel-gate-v1.json`。
- 固定检查：最少 10 个晋升样本、Candidate/Reranker Recall@5 与 MRR、Context Evidence Recall、
  分阶段失败率，以及相对上一基线的最大允许退化。
- `passed`：样本数和所有阈值均满足。
- `blocked`：样本充分但指标或失败率退化。
- `insufficient_evidence`：晋升样本不足；该状态不能发布。

`run_production_trace_eval.py --label-status promoted` 提供历史快照观察报告；正式发布使用
`run_rag_flywheel.py --replay-promoted --fail-on-blocked`，它先验证 manifest 中的 Trace ID、Query Hash、
隐私和晋升状态，再用当前生产 RetrievalService、Embedding 与 Reranker 重放，不复用历史排序分数。
`scripts/release-gate.sh p4` 把代码质量门与该真实数据质量门串联，任一失败即停止。

## 首轮真实运行

2026-08-02 使用当前本地 PostgreSQL 真实业务数据运行并收口：

- confirmed/promoted 观察样本：28。
- 初始 promoted 发布样本：0，自动脱敏审核队列 60，门禁正确返回 `insufficient_evidence`。
- 排除旧 v2 策略、重复问题和证据语义疑点后，人工晋升 10 条 v6 代表样本。
- `rag-promoted-p4-v1` 覆盖多文档比较、公式、表格、结构、Mask、FFN、位置编码、优化器、正则化与
  Candidate 召回边界，不包含原始 query 或 Chunk 正文。
- 当前生产 Pipeline 真实重放 10/10 完成：Candidate Recall@5 90%、MRR 100%，Reranker Recall@5
  97.5%、MRR 100%，Context Evidence Recall 100%。2 条候选漏召回和 2 条 Context truncated 作为
  已知边界保留，所有门禁检查通过。

初始证据不足是预期的安全行为：历史 28 条人工确认标签只能用于观察，不能自动冒充发布金标。首版
cohort 完成后，新增 promoted 标签也不会静默改变分母；必须显式更新版本化 manifest 并重新审核。

## 复现

```bash
cd apps/agent-worker
export JARVIS_DATABASE_URL=postgresql+asyncpg://...

python eval/runners/run_rag_flywheel.py --replay-promoted

# 后续版本与上一版飞轮快照比较，并在证据不足/回归时返回非零状态。
python eval/runners/run_rag_flywheel.py \
  --replay-promoted \
  --baseline eval/reports/flywheel/<previous>/flywheel.json \
  --fail-on-blocked

# 代码门禁 + promoted-only RAG 门禁。
JARVIS_DATABASE_URL=postgresql+asyncpg://... ../../scripts/release-gate.sh p4
```

本地报告位于 Git 忽略的 `eval/reports/flywheel/`。只有脱敏聚合结论进入版本文档，原始生产内容保留在
本地 PostgreSQL 业务真源中。

## P4-4 在线反馈回流

2026-08-02 新增用户在线反馈入口，但保持飞轮质量真相的人工边界：

```text
持久化 Assistant Message
-> 服务端解析 Run 与真实 RAG trace
-> 校验回答目标或本次 Context 内的具体引用
-> 幂等写入 rag_evaluation_feedback
-> Workspace 范围的脱敏审核队列
-> 人工标记已查看 / 忽略
-> 如需形成证据标签，继续使用既有隐私复核与 human label 流程
```

反馈类型为有帮助、没帮助、依据不足和指定引用有误。Web 不接触 trace_id，审核投影只显示 hash、版本、
计数和关联 ID；反馈提交与审核写 AuditLog。系统不会自动把点赞当正例、把点踩当难负例，也不会自动
批准隐私、创建 confirmed label 或改变 promoted manifest。
## P4-5 用户反馈诊断接入

用户反馈沿用同一 production trace 与 label 真源。自动部分包括候选入队、阶段证据投影、状态/失败类型
汇总；人工部分包括隐私复核、失败归因、证据选择、label 确认与 cohort 晋升。反馈证据选择最多形成
`user_feedback/draft`，不会被 release gate 当作 promoted 样本。飞轮快照新增 `feedback_summary`，按状态与
失败分类提供有界计数，仍排除 query、回答、Chunk 正文和向量。

## P4-6 人工审核与晋升控制台

Knowledge 页面复用 `RagEvaluationReviewService`，将原 CLI 的隐私复核、人工标签确认和晋升动作接入
受控 Web 流程。读取与写入均要求 Workspace；pending/rejected 详情不投影原始 query、请求参数或 Chunk
摘要，approved 详情最多返回 100 条证据和每条 320 字符摘要。每次状态变更写 AuditLog。

运行时 `promote` 只把已批准且 confirmed 的 label 更新为 promoted，并返回由 `trace_id + query_hash`
组成的脱敏候选。它不会调用评测 runner，也不会改写 `rag-promoted-p4-v1.json`。将候选纳入正式 cohort
仍必须经过人工检查、版本化 release commit、当前生产 Pipeline 重放和 P4 release gate；因此数据库候选
增长不会静默改变质量阈值的固定分母。

## P4-7 版本化基线与发布门禁

`eval/baselines/rag-promoted-p4-v1.json` 保存首版 cohort 的脱敏聚合指标，不保存样本原文、答案、Chunk
或向量。`scripts/release-gate.sh rag|p4` 默认加载该基线；文件缺失、当前绝对阈值不达标或相对基线退化
超限都会返回非零状态。环境变量仍可显式选择经过审核的其他版本基线，但不再需要调用者手工指定当前
默认版本。

2026-08-02 在候选 `d1a4ee9` 上执行完整 P4 门禁，11/11 步通过。当前生产 Pipeline 重放 10 条固定
样本：Candidate Recall@5 90%、MRR 100%，Reranker Recall@5 95%、MRR 100%，Context Evidence Recall
100%。相对首版基线 Candidate 与 Context 持平，Reranker Recall@5 下降 2.5 个百分点但仍在 3 个百分点
允许范围内；Context truncated 从 20% 降至 10%。该边界保留观察，不触发无证据的算法改造。
