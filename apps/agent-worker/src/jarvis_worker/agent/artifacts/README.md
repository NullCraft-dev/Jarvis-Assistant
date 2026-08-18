# Artifacts

`artifacts/` 负责大输出和文件产物的 metadata 与引用。

长文本、截图、diff、生成文件、大型工具结果不应无限塞进 RuntimeEvent 或 context，应作为 artifact 存储。

数据库保存 metadata 和引用；大 payload 的保存由 artifact adapter 负责。
