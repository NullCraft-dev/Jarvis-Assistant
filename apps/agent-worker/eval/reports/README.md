# Reports

评测运行生成的逐案例结果、版本差异和汇总报告放在本目录。除本说明外内容默认不进入 Git，避免把
文档正文、OCR 输出、模型响应或本地路径误提交。

每个 `<case-id>/<run-id>/` 至少包含：

1. `01-native-and-routing.json`
2. `02-structure-model-raw.json`
3. `03-preprocessed-fused.json`
4. `04-chunks.json`
5. `05-embeddings.json`（只保存 metadata/hash，不保存向量）
6. `06-retrieval.json`
7. `07-generation.json`
8. `report.json` 与 `report.md`

执行到较早阶段时，后续编号文件可以不存在，但 `report.json` 必须明确记录未执行或阻塞原因。
