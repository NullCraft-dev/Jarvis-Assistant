# P4 当前版本 RAG 质量基线

## 结论

状态：**P4-2 当前版本门禁通过（2026-08-01）**。

候选基于 Git `895d707` 加当前 P4 工作区改动，生产策略为 `rag-hybrid-retrieval-v6`、
`policy-selector-v2`、`fair-neighbor-multimodal-budget-v2`。本轮没有因为指标不理想而预先引入 BM25、
新 Query Rewrite 或新模型。

## 样本与方法

- 原 `production-rag-p0-v1`：12 个 Transformer 事实题；已升级为中英文、LaTeX 和等价公式分组金标。
- 扩展 `production-rag-p4-v1`：7 个自然用户问题，覆盖多文档、表格、公式、长文档、无答案、跨来源
  冲突和同名资料。
- 19 个 Task 全部通过真实 `Gateway -> Redis -> Agent Worker -> ToolGateway -> rag.search ->
  Storage` 链路执行；问题中不包含工具名、UUID、内部参数或强制输出协议。
- 最终答案按确定性事实组、Runtime 可信引用和拒答/澄清行为评分。17 个有引用样本经逐条人工复核后，
  使用现有 `rag_evaluation_traces -> privacy approved -> human_review confirmed label -> eval/framework`
  数据飞轮计算分阶段指标。无答案与同名澄清两项没有引用，正确保留在端到端分母中，不进入召回指标。
- 版本化文档只保留聚合指标、Trace ID 和版本，不包含原始 query、回答正文或 Chunk 正文。

## 门禁结果

| 指标 | 门禁 | 当前值 | 结论 |
|---|---:|---:|---|
| Task 完成率 | 100% | 100%（19/19） | 通过 |
| Candidate Recall@5 | >= 80% | 83.33% | 通过 |
| Candidate MRR | >= 80% | 84.66% | 通过 |
| Reranker Recall@5 | >= 90% | 95.59% | 通过 |
| Reranker MRR | >= 90% | 96.08% | 通过 |
| Context Evidence Recall | >= 95% | 100% | 通过 |
| 指定文档覆盖完整率 | 100% | 100% | 通过 |
| 回答正确性 | >= 95% | 100% | 通过 |
| 引用完整性 | >= 95% | 100% | 通过 |
| p95 端到端耗时 | <= 30 秒 | 17.975 秒 | 通过 |

补充指标：Candidate Recall@10 为 92.16%，Reranker evidence drop rate 为 4.41%；端到端耗时中位数
13.691 秒，最小 6.534 秒，最大 39.506 秒。17 条分阶段样本中有 5 条触发 `candidate_evidence_missed`
候选、5 条 Context 标记 truncated，但最终 Context Evidence Recall 仍为 100%，没有形成答案或引用失败。

## 本轮修复与判断

- 复杂度表问题连续两次在 Intent 阶段失败。根因是身份校验把 `recurrent layer` 等普通查询词误当成
  文档名；修复后只把 arXiv 编号、书名号标题和 ResNet/MobileNet 等强身份词用于确定性映射。
- 多份同名 MobileNet 文档曾被任意选择。现在强身份命中多份文档时，Runtime 直接把 `selected`
  安全降级为 `unresolved`，不依赖 Intent LLM 重试；回复只请用户按标题、来源、版本或上传时间区分，
  不索要 `document_id`、UUID、工具名或内部参数。
- Candidate 与 Reranker 已通过当前门禁，Context 完整保留最终证据，因此本轮不引入 BM25 或新重排
  算法。`candidate_evidence_missed` 和 4.41% evidence drop 进入下一轮定向样本审查；只有固定金标证明
  候选漏召回或重排持续丢证据时再改算法。
- 官方历史+当前数据飞轮聚合共有 28 条确认样本，包含旧 v2-v5 策略，不能替代本页的 v6 当前候选
  门禁；它用于观察长期趋势，当前 revision 结论只使用上述 17 条 v6 人工确认 trace。

## 复现

```bash
cd apps/agent-worker
python eval/runners/run_production_p0.py --workspace-id <workspace-id>
python eval/runners/run_production_p4.py --workspace-id <workspace-id>

# 人工逐条核验答案事实与引用后，才允许写入现有 trace/label 数据飞轮。
python eval/runners/run_p4_quality_baseline.py \
  --run-report <p0-run.json> \
  --run-report <p4-run.json> \
  --confirm-reviewed-citations \
  --output <baseline.json>

python eval/runners/run_production_trace_eval.py --limit 500
```

详细 JSON 报告继续写入 Git 忽略的 `eval/reports/` 或本地受控路径，避免把原始生产内容提交到仓库。
