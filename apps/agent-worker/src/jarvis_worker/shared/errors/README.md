# Errors

`errors/` 负责 Python 侧结构化错误基础类型。

当 Python worker 需要共享错误层时，AppError、error code、recoverable、category、safe message mapping 可以放在这里。

原始异常、堆栈和敏感细节不应跨进程或透传到 UI。
