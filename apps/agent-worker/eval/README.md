# RAG Evaluation

本目录是与 `src/`、`tests/` 同级的独立 RAG 质量评测系统。它不进入 `jarvis_worker` 生产包，
也不属于验证代码可行性的 pytest 测试集；生产 RAG 的唯一领域 owner 仍是
`src/jarvis_worker/agent/rag`。

## 目标

- 用真实、可追溯的文档衡量解析、阅读顺序、结构恢复和多模态关联质量。
- 用人工金标衡量 chunk 边界、语义完整性、来源定位和确定性。
- 复用同一 corpus 和 query/evidence gold 评估 Embedding、检索、重排和最终 RAG 回答。
- 将实际失败样本脱敏后回流为固定回归案例，形成可持续的数据飞轮。
- 保存 parser、chunker、embedding model、index 和 generation model 版本，使结果可复现。

## 目录

```text
eval/
├── manifests/       # 数据集版本和案例清单
├── cases/           # 来源、许可、覆盖标签和金标引用
├── annotations/     # 页面、节点、关系、分块、query 与回答金标
├── corpus/          # 本地公开缓存、私有或生成的原始文档
├── schemas/         # case 与 annotation JSON Schema
├── tasks/           # 各阶段输入、输出与指标定义
├── runners/         # 数据校验、可重复下载与 pipeline runner
├── tests/           # 只测试评测框架自身
└── reports/         # 本地生成结果，不提交 Git
```

## 案例生命周期

- `planned`：来源和覆盖目标已登记，原文或金标尚未准备完成。
- `annotated`：原文、SHA-256 和 annotation 均存在，可以进入开发评测。
- `verified`：annotation 已经独立复核，可进入固定回归或盲测。
- `quarantined`：存在授权、隐私或金标质量问题，禁止参与分数汇总。

不要通过修改金标迎合当前实现。发现歧义时记录可接受边界和判定依据。

## 数据分层

- `public`：许可明确的公开文档，必须记录来源、下载 URL、许可证和 attribution。
- `private`：用户文档或运行失败样本，默认不进入 Git，提升为共享案例前必须脱敏。
- `generated`：由仓库脚本确定性生成的边界样本，记录 generator 和版本。

公开 PDF 由 `runners/fetch_corpus.py` 下载到被 Git 忽略的本地 cache。Git 只保存可复现 URL、
安全相对路径和 SHA-256；需要离线共享时使用受控对象存储或 Git LFS，不把大型二进制写入普通 Git
历史。不得收集含个人信息、密钥、内部合同或许可不明的文档。

## 使用

```bash
cd apps/agent-worker
python eval/runners/fetch_corpus.py
python eval/runners/validate_dataset.py
pytest -q eval/tests
```

正式评测以完整链路 runner 为入口，不以 native-only 诊断作为结论：

```bash
# PaddleOCR 客户端使用隔离重型运行时；MLX-VLM 必须已在 127.0.0.1:8111 启动。
cd <repo-root>
PYTHONPATH=apps/agent-worker/src \
  .local/rag-runtimes/paddleocr-client/.venv/bin/python \
  apps/agent-worker/eval/runners/run_pipeline_eval.py \
  --case-id joss-peat-2025 --through chunking

# 修改金标匹配或指标算法后，复用已保存的 VL/融合/Chunk 产物重新评分。
cd apps/agent-worker
python eval/runners/rescore_pipeline_eval.py \
  eval/reports/joss-peat-2025/<run-id>

# 从缓存 Chunk 继续执行 Embedding、检索和生成，不重复昂贵的视觉推理。
python eval/runners/continue_pipeline_eval.py \
  eval/reports/joss-peat-2025/<run-id> --through generation --top-k 5

# 从隐私已批准、标签已确认的真实生产轨迹生成 JSON + Markdown 报告。
# 运行前需按开发指南在当前 shell 注入 JARVIS_DATABASE_URL；本地 .env 也会被加载。
python eval/runners/run_production_trace_eval.py --limit 100
```

### P4-3 自动数据飞轮

生产 `rag.search` 仍自动采集 trace。以下单入口自动完成脱敏候选挖掘、confirmed/promoted 观察评估、
promoted-only 发布门禁和版本基线对比；它不会自动批准隐私、伪造证据标签或晋升样本：

```bash
cd apps/agent-worker
export JARVIS_DATABASE_URL=postgresql+asyncpg://...

python eval/runners/run_rag_flywheel.py --replay-promoted

# 发布时要求门禁实际通过；证据不足和指标回归均返回非零状态。
python eval/runners/run_rag_flywheel.py \
  --replay-promoted \
  --baseline <previous-flywheel.json> \
  --fail-on-blocked

# 完整 P4 代码质量 + 数据质量门禁。
JARVIS_DATABASE_URL=postgresql+asyncpg://... ../../scripts/release-gate.sh p4
```

仓库内的脱敏聚合基线位于 `eval/baselines/rag-promoted-p4-v1.json`。`scripts/release-gate.sh rag|p4`
默认自动加载该文件并与当前重放结果比较；基线缺失时失败关闭。只有显式提供
`JARVIS_RAG_FLYWHEEL_BASELINE` 才会覆盖默认路径。版本化基线不得包含原始 query、回答、Chunk 正文或
Embedding vector。

自动审核队列只包含 Trace ID、Query Hash、状态、优先级与失败类型，不输出原始 query、回答或 Chunk
正文。发布门禁只消费 `promoted` 标签；`confirmed` 样本用于观察与候选晋升，不能直接放行版本。
正式重放 cohort 固定在 `manifests/rag-promoted-p4-v1.json`，只保存 Trace ID、Query Hash 和类别；
runner 会拒绝缺失、未晋升、Hash 不一致或重复的样本，并用当前生产 RetrievalService 重新检索和重排。

### 真实链路 P0 基线

`tasks/production-rag-p0-v1.json` 提供首批 12 个来自真实公开论文的事实型问题。执行器从 Gateway 创建
真实 Task，等待 Python Agent 调用生产 `rag.search`，并保留 Task/Run/Conversation 血缘和最终回答；
它不直接调用某个解析器、Embedding 或检索实现。

```bash
cd apps/agent-worker
python eval/runners/run_production_p0.py \
  --workspace-id <workspace-id> \
  --timeout-seconds 180
```

P4-2 扩展集与当前版本聚合入口：

```bash
python eval/runners/run_production_p4.py --workspace-id <workspace-id>

# 只有人工逐条核验最终答案事实与引用后，才可显式确认并写回现有 trace/label 数据飞轮。
python eval/runners/run_p4_quality_baseline.py \
  --run-report <p0-run.json> \
  --run-report <p4-run.json> \
  --confirm-reviewed-citations \
  --output <baseline.json>
```

`--reuse-existing` 可按完全相同的自然用户目标从 Gateway 业务真源恢复最近终态任务，用于修复报告落盘
或重评分，不重新调用模型。它不能绕过人工引用复核，也不能把无引用的拒答/澄清样本塞入召回分母。

运行结果写入 Git 忽略的 `eval/reports/production-p0/`。随后通过审核 CLI 按 `task_id/run_id` 找到轨迹，
完成隐私审核与证据标注，再生成分阶段报告。Task 失败也属于完整链路的评估结果，不能从成功率分母中
删除；没有产生 `rag.search` trace 的失败需单独归入 Runtime/Tool 参数阶段。

### 开发者内部审核 CLI

审核流程不依赖用户点赞/点踩，也不会自动把检测结果当成金标：

```bash
cd apps/agent-worker
export JARVIS_DATABASE_URL=postgresql+asyncpg://...

# 列表不输出原始 query。
python eval/runners/review_production_traces.py list --privacy pending

# 显式查看 query、Candidate/Reranker/Context 和有界 Chunk 预览。
python eval/runners/review_production_traces.py inspect <trace-id>

# Candidate 没有正确证据时，浏览同一 Workspace 的真实文档与分块。
python eval/runners/review_production_traces.py documents <trace-id>
python eval/runners/review_production_traces.py chunks <trace-id> <document-id>

# 先完成隐私复核，再确认正例/难负例。
python eval/runners/review_production_traces.py approve <trace-id>
python eval/runners/review_production_traces.py label <trace-id> \
  --positive <chunk-id> \
  --hard-negative <chunk-id> \
  --notes "人工复核依据"

# 生成分阶段报告；确认稳定后晋升为本地回归候选。
python eval/runners/review_production_traces.py evaluate
python eval/runners/review_production_traces.py promote <trace-id>
```

`reject` 用于拒绝包含敏感信息或不适合进入评估集的轨迹。`promote` 只接受隐私已批准且标签已确认的
轨迹，导出的本地候选包含 query、标签、版本和 Chunk 来源元数据，但不包含 Chunk 正文；文件默认写入
被 Git 忽略的 `eval/reports/promotion-candidates/`。已晋升标签不可通过 `label` 命令直接覆盖。

每次运行保存七层中间产物：native/routing、PaddleOCR-VL raw、融合节点、Chunk、Embedding
metadata、检索结果和生成答案。二进制图片、向量和 API key 不写入报告。任一阶段失败时，下游标记为
`blocked-by-upstream`，禁止静默退化。

当前生产代码已提供预处理、分片、Embedding、混合召回、Reranker 和 Context Assembly owner。每次真实
`rag.search` 会把不含正文的候选排序、重排结果、最终 context chunk 和版本写入
`rag_evaluation_traces`，并以 `evaluation_trace_id` 关联后续用户反馈。只有通过隐私复核且拥有
`confirmed/promoted` 标签的轨迹，才能由 `eval/framework` 投影为评估样本；自动检测结果不能直接充当
金标。现有文件 runner 的 Retrieval/Generation adapter 仍用于历史 corpus 实验，不得冒充生产检索或
尚未落地的生产 Generation owner。

真实数据飞轮顺序为：生产检索轨迹采集 → 用户反馈/人工复核 → 隐私批准 → 正例与难负例标签确认 →
分阶段指标与失败归因 → 晋升为固定回归案例 → 算法修改前后对比。原始 query、候选快照和待复核标签
保留在本地 PostgreSQL 业务真源，不自动提交到 Git，也不自动发送给外部 Judge。

只有 `verified` 案例可以作为发布质量门；`planned` 不参与评分，`quarantined` 不执行。Embedding、
检索和最终回答使用同一 query/evidence 金标，但分别报告指标，不能用最终回答分数掩盖上游问题。
