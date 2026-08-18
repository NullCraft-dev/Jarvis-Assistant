# Context

`context/` 负责构造每次模型调用的输入上下文。

Context builder 可以选择 task state、最近消息、工具观测、文件摘要和已确认的 memory 片段。

这里不直接持久化业务真相，也不执行工具。
