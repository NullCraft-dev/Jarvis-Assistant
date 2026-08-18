# RAG evaluation stages

同一份 corpus 由完整链路主评测执行，再按阶段独立评分，以便定位退化发生在哪一层。发布结论来自
端到端运行；单阶段运行只用于诊断、调参和缓存续跑。

| 阶段 | 固定输入 | 主要指标 |
|---|---|---|
| `preprocessing` | PDF + 页面/节点金标 | 文本覆盖、阅读顺序、类型、bbox、表格、关系、路由准确率 |
| `chunking` | 预处理结果 + 边界金标 | 超限率、错误切断、表头保留、证据完整性、来源定位、确定性 |
| `embedding` | 固定 chunk + query/evidence gold | 相似度间隔、Recall@K、MRR、跨语言与近义表达鲁棒性 |
| `retrieval` | 固定索引 + query/evidence gold | Recall@K、MRR、nDCG、过滤正确率、重复率、延迟 |
| `generation` | 固定检索证据 + 回答事实金标 | 事实覆盖、引用正确率、无依据陈述率、拒答准确率 |
| `end_to_end` | PDF + query/answer gold | 最终正确率、证据召回、引用归因、总延迟和失败归因 |

Embedding 与 retrieval 可以共享 query，但必须保存 provider/model、维度、归一化、chunker 和索引
版本。端到端报告必须同时保留各阶段子分数，禁止只输出一个总分。

当前 production owner 边界：

- `preprocessing/chunking/embedding` 可直接调用生产实现。
- `retrieval/generation` 在生产 owner 落地前只能使用显式 eval adapter，报告必须保留
  `production_retrieval_service_exercised=false` 和 `production_chain_complete=false`。
- 修改指标算法时优先对缓存产物 rescore；修改 parser、路由、融合或 chunker 时必须重新运行 PDF。
