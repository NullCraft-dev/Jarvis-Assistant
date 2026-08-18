"""RuntimeEvent 类型定义 — 与 Go 侧 dto/types.go 和 shared/src/types.ts 对齐。

真源：docs/13-interface-contract.md § Runtime Events

约束：
- 不重新定义 RuntimeEvent shape
- event_id == runtime_event.id
- envelope 层不覆盖 runtime_event 字段
"""

# Terminal event types — 收到后 run 生命周期结束
TERMINAL_EVENT_TYPES = frozenset({
    "agent.run.completed",
    "agent.run.failed",
    "agent.run.cancelled",
})

# 所有 RuntimeEventType 枚举值（文档用，运行时不做校验）
ALL_EVENT_TYPES = frozenset({
    "task.created",
    "task.updated",
    "agent.run.started",
    "agent.run.paused",
    "agent.run.resumed",
    "agent.run.completed",
    "agent.run.failed",
    "agent.step.started",
    "agent.step.updated",
    "agent.step.completed",
    "agent.step.failed",
    "model.call.started",
    "model.context.prepared",
    "model.delta",
    "model.call.completed",
    "model.call.failed",
    "tool.call.started",
    "tool.call.finished",
    "tool.call.failed",
    "mcp.call.started",
    "mcp.call.finished",
    "mcp.call.failed",
    "permission.required",
    "permission.resolved",
    "permission.expired",
    "artifact.created",
    "log.appended",
})
