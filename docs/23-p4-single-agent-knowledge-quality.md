# P4 单 Agent 知识研究质量收口

## 目标

在 RC1、P2 可靠性安全加固和 P3 Web 体验收口之后，继续提升单 Agent 的知识研究质量。P4 不以增加
工具数量为目标，而是让“指定多份资料 -> 可信检索 -> 综合回答 -> 写入 report/note”成为可量化、
可解释、可恢复的完整链路。

P4 当前不进入桌面端、Multi-Agent、插件市场或第三方 Skill 供应链。只有单 Agent 的检索、引用、
多文档综合和知识写入质量达到门禁后，才重新评估是否需要复杂编排。

## P4-0 真源与门禁

状态：**已完成首轮真源审计（2026-08-01）**。

- Phase 6 的真实模型与本地工具 MVP 链路已经完成；LangChain adapter 保留为可选兼容工作，不再阻断
  当前产品阶段。
- P2 已完成审计导出、保留策略与安全清理演练；P3 已完成 RAG 文档详情、版本与批量运维。
- 已有 Intent Layer、匿名文档目录、Runtime-owned `document_ids`、RAG 评估 trace/label 和
  RAG -> Knowledge provenance，不重复建设第二套任务或证据协议。
- P4 质量改动必须复用现有 `RagQueryRewriter -> CandidateRetriever -> Reranker ->
  ContextAssembler` 端口，并以生产评测数据判断是否引入 Query Rewrite、BM25 或新模型。

## P4-1 多文档研究闭环

用户可在自然语言中点名一份或多份当前 Workspace 的 ready RAG 文档。P4 验收不能只使用工具名、
文件 ID、精确标题和输出字段齐全的脚本式指令；底层契约测试与自然用户旅程必须分开通过。

`intent-llm-v3` 只向 Intent LLM 提供匿名 `doc_N`、标题、创建时间与最多 600 字符的首 Chunk 身份
摘要，不暴露数据库 UUID。解析器将匿名键映射为可信 UUID，并校验用户问题里能唯一命中的英文名或
arXiv 编号；错配直接拒绝候选，不能拿相近论文继续回答。AgentRunner 覆盖模型提交的
`document_ids`，不能跨 Workspace、不能把未解析指代降级为全库检索。

多文档检索增加以下不变量：

- `rag.search` 的有效 `top_k` 不得小于已选文档数，上限仍为 20。
- 最终 Policy 在候选可用时优先为每份已选文档保留至少一条主证据，再按相关性、去重和单文档配额
  填充剩余位置。
- ToolResult 返回 `document_coverage`：`requested_count/covered_count/complete/
  uncovered_document_ids`。覆盖不完整时，Agent 不得声称已完成全面比较或总结。
- `knowledge.create_document` 的 provenance 只连接本 Run 成功 `rag.search` 中实际进入模型上下文的
  Artifact、RAG Document、ToolCall 和 Chunk；多文档关系稳定去重且总量不超过 50。
- 多文件总结的标题概括共同主题、比较问题或报告周期，不拼接文件名列表，不加入 Run ID、revision、
  随机后缀或验证阶段。
- 自然语言验收至少覆盖“知识库里 X 和 Y 那两篇论文”这类表达；系统必须自行解析资料、选择工具和
  形成答案。只有在用户明确要求保存时才触发 Knowledge 写入，不要求用户提供工具名、内部参数、
  UUID、provenance 或标题算法。

验收链路：

```text
用户点名多份资料并要求总结/比较、保存 report 或 note
-> Intent 选择多个匿名文档键
-> Runtime 注入可信 document_ids
-> rag.search 返回每份文档的证据覆盖与安全缺口
-> 模型基于可见证据生成综合正文和可信 citations
-> knowledge.create_document 经 L2 单次确认写入 Obsidian
-> Markdown 使用纯语义标题并保存多文档 provenance
-> 刷新后 Task/Run/ToolCall/Permission/Artifact/Knowledge 状态一致
```

## P4-2 当前版本质量基线

状态：**已完成并通过当前版本门禁（2026-08-01）**。聚合结果、阈值、失败归因与复现命令见
`docs/24-p4-rag-quality-baseline.md`。

P4-1 自动化与真实页面闭环通过后，在同一 revision 重新运行 `production-rag-p0-v1`，并扩展多文档、
表格、公式、长文档、无答案、冲突证据和同名资料样本。至少记录 Candidate Recall/MRR、Reranker
Recall/MRR、Context Evidence Recall、引用完整性、回答正确性、覆盖完整率、失败类型与耗时。

算法改动按证据进入：候选漏召回再评估 BM25/Query Rewrite，重排丢证据再调整融合，Context 丢证据
再调整预算与冲突策略。不得先实现算法再寻找理由，也不得用合成分数替代真实生产链路。

## 完成标准

- 多文档范围、覆盖状态、引用和 Knowledge provenance 有自动化契约测试。
- 至少一条真实 Web 多文档旅程完成，且覆盖完整、引用可信、知识文档标题与分类正确。
- 当前 revision 的生产 RAG 基线可重复运行并形成版本化报告。
- 质量门禁通过前，不进入桌面端或 Multi-Agent。

## P4-3 数据飞轮发布闭环

状态：**已完成并通过首版 promoted 固定回归集真实重放（2026-08-02）**。

生产 RAG 继续自动记录不含正文的阶段 trace；`run_rag_flywheel.py` 自动生成脱敏审核队列、评估全部
confirmed/promoted 样本，并只使用 promoted 样本执行发布门禁。门禁同时检查绝对阈值、相对上一版
最大退化幅度、失败类型比例和最小样本数。缺少 promoted 金标时必须返回 `insufficient_evidence`，
不能把普通 confirmed 标签视为发布通过。

首版 `rag-promoted-p4-v1` 固化 10 条跨多文档、公式、表格、结构和训练配置样本。正式门禁不读取历史
排序分数，而是用当前 `RagRetrievalService`、Embedding 和 BGE Reranker 自动重放，随后再与绝对阈值
和上一版本基线比较。首次真实重放全部完成并通过门禁。

隐私批准、正例/难负例确认和晋升是刻意保留的人类决策点；自动化只能排序与建议，不能自行把模型
引用当成真相。完整契约、命令和首轮真实结果见 `docs/25-p4-rag-data-flywheel.md`。

## P4-7 质量发布收口

状态：**完整 P4 门禁已通过（2026-08-02）**。

当前候选版本依次通过共享契约、Gateway test/vet、Web test/build、Agent Worker 全量测试、Python
质量/编译和 promoted-only RAG 重放共 11 项门禁。10 条固定 cohort 的 Candidate Recall@5 为 90%、
MRR 100%，Reranker Recall@5 为 95%、MRR 100%，Context Evidence Recall 100%，没有 Context 证据丢失。

首版重放指标已保存为不含原文的版本化聚合基线。后续 `scripts/release-gate.sh rag|p4` 默认比较该基线，
缺失时失败关闭；更新基线必须和 cohort/策略变更一起人工审核并提交。P4 完成后仍不因 pending 候选数量
自动扩充 cohort，也不因局部指标波动直接引入新算法。
