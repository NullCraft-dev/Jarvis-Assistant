// Package bus 定义 RuntimeBus 和 RuntimeStateStore 接口。
//
// Handler 只依赖这两个接口，不直接访问 InMemoryState、mock 包或 Redis。
//
// # 职责边界
//
// 2B-0 将 2A 的合并接口拆分为两个独立抽象：
//   - RuntimeBus：运行通信语义（run 准备、事件存取、权限决策路由）
//   - RuntimeStateStore：临时 in-memory 状态查询（仅 mock 阶段，未来由 Storage 层承担）
//
// # 约束
//
//   - Redis Runtime Bus 不得成为 Task / Run / Step / ToolCall / Permission / AuditLog 的业务真源
//   - RuntimeStateStore 的 GetRun / GetTask / ListTasks / UpdateRunStatus 是 2A/2B-0 mock
//     阶段的临时方法，不代表未来 RedisRuntimeBus 的最终接口
//   - 2B-1 接入 Redis 时，RuntimeBus 方法对应 queue/command/event 通信；
//     task/run 的持久化查询走 Storage 层，不应通过 RuntimeBus 暴露
//
// 真源：docs/13-interface-contract.md § Internal Runtime Bus Contract
package orchestrator

import "github.com/jarvis-assistant/gateway/internal/contracts"

// RuntimeBus 承载运行通信语义。
//
// 当前 InMemoryRuntimeBus 复用现有 mock event 行为；
// 2B-1 接入 Redis 时，本接口的方法对应 queue enqueue / event 订阅 / permission command 路由。
//
// 职责：
//   - run 准备与初始事件生成
//   - 事件存取
//   - 权限决策路由与后续事件生成
//
// 不负责：
//   - Agent loop 执行
//   - LLM 调用
//   - 工具执行
//   - task/run 状态持久化查询（走 RuntimeStateStore → 未来 Storage 层）
//   - SSE 推送（由 handler 负责）
//   - 作为 Redis / 业务数据的真源
type RuntimeBus interface {
	// PrepareRun 为 CreateTask 创建 task + run 并生成初始事件序列。
	// 当前 InMemory 实现直接生成 mock 事件并存入内存。
	PrepareRun(input contracts.CreateTaskInput) (*contracts.TaskDTO, *contracts.AgentRunDTO, []contracts.RuntimeEvent, error)

	// GetEvents 返回 run 当前所有已产生事件的深拷贝。
	// 若 run 存在但尚无事件（如旧 run 被 SSE 订阅），自动生成默认事件。
	// 若 run 不存在，返回 error。
	GetEvents(runID contracts.ID) ([]contracts.RuntimeEvent, error)

	// ResolvePermission 处理权限决策，返回权限请求深拷贝和后续事件深拷贝。
	// 内部完成：查找权限请求 → 生成 resolved + post 事件 → 追加到 run 事件列表 → 更新 run 状态。
	ResolvePermission(decision contracts.PermissionDecisionDTO) (*contracts.PermissionRequestDTO, []contracts.RuntimeEvent, error)

	// CancelRun 处理取消运行命令（3C cancel）。
	//
	// InMemory 模式：直接更新 run 状态为 cancelled。
	// Redis 模式：向 worker-command stream 发布 run.cancel command，
	// 由 Python worker 确认后发出 agent.run.cancelled RuntimeEvent。
	// Gateway 不直接生成 agent.run.cancelled。
	//
	// run_id 不存在时返回 error。
	CancelRun(runID contracts.ID) (*contracts.AgentRunDTO, error)
}

// PumpCloser 控制 event pump 的生命周期。
//
// redis 模式下，EventPump 从 Redis runtime event stream 读取 worker 事件
// 并追加到 InMemoryRuntimeBus，使 SSE 能读取到 worker 产生的事件。
//
// inmemory 模式下返回 nil（无 pump）。
//
// 职责：
//   - Start 创建 consumer group 并启动后台 pump goroutine
//   - Close 取消 pump context 并等待 goroutine 退出
//
// 不负责：
//   - 读取事件的业务语义
//   - SSE 推送
//   - 成为业务真源
type PumpCloser interface {
	// Start 创建 consumer group（幂等）并启动后台 pump goroutine。
	// 返回 error 仅当 consumer group 创建失败。
	Start() error

	// Close 取消 pump 的 context 并等待 goroutine 退出。
	// 多次调用安全（cancel 幂等）。
	Close() error
}

// RuntimeStateStore 是 2A/2B-0 的临时 in-memory 状态查询接口。
//
// 当前 InMemoryRuntimeBus 同时实现本接口；2B-1 之后本接口的方法应
// 由 Storage-backed 的 StateStore 替代，不再与 RuntimeBus 共享实现。
//
// 职责（临时）：
//   - task/run 的内存查询与状态更新（仅 mock / in-memory 阶段）
//
// 不负责：
//   - Storage 持久化（当前切片暂不持久化）
//   - 作为 Redis / 业务数据的真源
//   - 运行通信语义（走 RuntimeBus）
type RuntimeStateStore interface {
	// GetRun 返回 run 的拷贝；ok=false 表示不存在。
	GetRun(runID contracts.ID) (*contracts.AgentRunDTO, bool)

	// UpdateRunStatus 更新 run 状态。
	UpdateRunStatus(runID contracts.ID, status string)

	// GetTask 返回 task 的拷贝；ok=false 表示不存在。
	GetTask(taskID contracts.ID) (*contracts.TaskDTO, bool)

	// ListTasks 返回所有 task 的值拷贝。
	ListTasks() []contracts.TaskDTO
}
