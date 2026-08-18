# Annotations

每个 annotation 使用 `../schemas/annotation.schema.json`，文件名建议为 `<case_id>.json`。

金标应优先覆盖困难页面，不要求第一轮逐字标完整份文档。`queries` 同时服务 Embedding、retrieval、
generation 和端到端 RAG，必须把 evidence 绑定到页面节点 gold ID；不可回答问题的 evidence 为空，
用于测试拒答。`verified` 案例必须填写独立 reviewer、reviewed_at，并将 review status 设为
`reviewed`。不确定边界应记录可接受集合，不要强行制造唯一答案。

`text_gold_mode=exact` 才允许计算字符错误率；`semantic` 只评估结构和证据语义，`mixed` 表示标题、
图注等短文本是精确转录，但长段落是人工语义摘要。评测器不得拿 semantic/mixed 摘要计算 CER。
