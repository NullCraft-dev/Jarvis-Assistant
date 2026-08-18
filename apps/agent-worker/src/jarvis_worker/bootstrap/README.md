# Bootstrap

`bootstrap/` 负责 Python worker 的进程组装和依赖接线，是当前 Python 后端的 composition root。

这里可以创建 runtime component、tool registry、model provider、storage adapter、Redis adapter 等对象，然后把它们注入真实运行路径。

这里不放 Agent 推理逻辑、不执行工具、不做存储决策，也不承载业务规则。
