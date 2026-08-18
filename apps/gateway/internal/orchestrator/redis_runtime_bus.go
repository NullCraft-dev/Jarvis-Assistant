// RedisRuntimeBus 是 Redis-backed RuntimeBus 接线骨架。
//
// 同时实现 RuntimeBus 和 RuntimeStateStore 接口。
//
// # 职责
//
//   - 组合 InMemoryRuntimeBus 作为临时 state owner：创建 task/run（最小初始状态）、
//     查询事件、权限对象读取与提交（仅 in-memory）
//   - 组合 runtimeredis.RedisRuntimeTransport 作为 Redis 通信层：
//     PrepareRun → EnqueueRunJob、ResolvePermission → PublishPermissionDecision
//   - 维护 run_id → trace_id 映射，保证 RunJobMessage 与 PermissionDecisionCommand
//     的 trace_id 一致
//   - RuntimeStateStore 方法全部委托 InMemoryRuntimeBus
//
// # 不负责
//
//   - 成为 Task / Run / Step / ToolCall / Permission / AuditLog 的业务真源
//   - 实现 Storage 持久化
//   - 启动 Python worker 或执行 Agent loop / LLM / 工具
//   - 替换默认 InMemoryRuntimeBus（main.go 默认路径不变）
//
// # 2B-2c event fan-out
//
//   - redis 模式下组合 EventPump，从 RuntimeEventReader 读取 Redis runtime event stream
//   - worker 产生的 RuntimeEvent 由 EventPump 追加到 InMemoryRuntimeBus，
//     然后 GetEvents（委托 in-memory）可读到这些事件，SSE 可见
//   - inmemory 模式下 eventPump 为 nil，不启动后台 goroutine
//
// # 错误语义
//
//   - PrepareRun：Redis EnqueueRunJob 失败时返回 error。task/run 已创建最小初始状态
//     （仅 task.created），但调用方通过 error 获知 Redis 通信失败
//   - ResolvePermission：ReservePermissionRequest 原子占用 pending →
//     PublishPermissionDecision 到 Redis → 成功则 CommitPermissionDecisionAckFromReserved
//     提交 ack，失败则 RestorePermissionRequest 恢复 pending 可重试。
//     并发重复请求只有一个能 reserve 成功，不会重复 publish。
//
// # 约束
//
//   - Redis 只承载 run job / command / event stream，不是业务数据真源
//   - 当前在没有 Storage 层的情况下，InMemoryRuntimeBus 作为临时 state owner
//   - PrepareRun 只生成最小初始事件（task.created），不生成任何 worker 完成事件
//   - trace_id 在 PrepareRun 时生成并存入内部 map，ResolvePermission 时复用
//   - 2B-2c：redis 模式下通过 EventPump 做后台 goroutine + event fan-out；真实 Redis 连接由 factory 注入
//
// 真源：docs/13-interface-contract.md § Internal Runtime Bus Contract
package orchestrator

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

// RedisRuntimeBus 是 Redis-backed RuntimeBus 接线骨架。
//
// 同时实现 RuntimeBus、RuntimeStateStore 和 PumpCloser 接口。
//
// 内部组合：
//   - InMemoryRuntimeBus：临时 state owner（task/run/events/permission 的内存存储）
//   - runtimeredis.RedisRuntimeTransport：Redis 写入通信层
//   - eventPump：redis 模式下从 RuntimeEventReader 读取 worker 事件并写入 inMemory
//   - heartbeatPump：redis 模式下从 HeartbeatReader 读取 worker 心跳并更新 WorkerStatusView
//   - workerStatusView：worker 状态内存视图（3B heartbeat）
//   - traceIDs：run_id → trace_id 临时映射（Storage 落地后迁移到持久化状态）
//
// 方法映射：
//
//	PrepareRun          → in-memory 最小初始状态 + 存储 trace_id + Redis EnqueueRunJob
//	GetEvents           → 委托 in-memory（通过 EventPump 注入的 worker 事件也在此可见）
//	ResolvePermission   → reserve pending → Redis PublishPermissionDecision → ack / restore
//	GetRun / GetTask / ListTasks / UpdateRunStatus → 委托 in-memory
//	GetWorkerStatuses    → WorkerStatusView.GetAll（3B heartbeat）
//	Start / Close       → EventPump + HeartbeatPump 生命周期
type RedisRuntimeBus struct {
	inMemory  *InMemoryRuntimeBus
	transport *runtimeredis.RedisRuntimeTransport
	eventPump *eventPump // nil 表示 inmemory 模式或无 reader

	// 3B: heartbeat
	heartbeatPump    *heartbeatPump
	workerStatusView *WorkerStatusView
	diagnostics      runtimeredis.RuntimeDiagnosticsReader

	mu       sync.RWMutex
	traceIDs map[contracts.ID]contracts.ID // run_id → trace_id，后续迁移到持久化状态
}

// NewRedisRuntimeBus 创建 Redis-backed RuntimeBus 接线骨架。
//
// transport 由调用方注入（测试用 fake client，生产用 GoRedisStreamClient）。
// eventReader 为 nil 时不创建 eventPump（inmemory-only 模式）。
// streamReader 用于 consumer group 创建（仅 eventReader 非 nil 时使用）。
// backoff 用于 pump 的失败退避。
//
// 3B heartbeat:
//   - heartbeatReader 为 nil 时不创建 heartbeat pump（inmemory-only 模式）
//   - staleTimeout 设置 worker 失联阈值，<= 0 使用默认 9s
//
// 若 transport 为 nil 则返回 error。
func NewRedisRuntimeBus(
	transport *runtimeredis.RedisRuntimeTransport,
	eventReader *runtimeredis.RuntimeEventReader,
	streamReader runtimeredis.RedisStreamReader,
	backoff EventPumpBackoff,
	heartbeatReader *runtimeredis.HeartbeatReader,
	staleTimeout time.Duration,
	consumerNames ...string,
) (*RedisRuntimeBus, error) {
	if transport == nil {
		return nil, fmt.Errorf("bus: cannot create RedisRuntimeBus with nil RedisRuntimeTransport")
	}

	rb := &RedisRuntimeBus{
		inMemory:         NewInMemoryRuntimeBus(),
		transport:        transport,
		traceIDs:         make(map[contracts.ID]contracts.ID),
		workerStatusView: NewWorkerStatusView(staleTimeout),
	}

	// 仅当 eventReader 非 nil 时创建 eventPump（redis 模式）
	if eventReader != nil {
		rb.eventPump = newEventPump(
			eventReader, streamReader, rb.inMemory, backoff, consumerNames...,
		)
	}

	// 3B: 仅当 heartbeatReader 非 nil 时创建 heartbeat pump
	if heartbeatReader != nil {
		hbBackoff := NewExponentialBackoff(100*time.Millisecond, 5*time.Second)
		rb.heartbeatPump = newHeartbeatPump(
			heartbeatReader, rb.workerStatusView, hbBackoff, consumerNames...,
		)
	}

	return rb, nil
}

// RequestMcpDiscovery 只发布管理命令；Gateway 不读取 MCP 配置，也不执行协议发现。
func (b *RedisRuntimeBus) RequestMcpDiscovery(ctx context.Context) (string, error) {
	commandID := uuid.NewString()
	cmd := runtimeredis.McpDiscoveryRefreshCommand{
		CommandID: commandID, TraceID: uuid.NewString(),
		Type: "mcp.discovery.refresh", RequestedAt: contracts.NowISO(),
		SchemaVersion: runtimeredis.SchemaVersion,
	}
	if err := b.transport.PublishMcpDiscoveryRefresh(ctx, cmd); err != nil {
		return "", err
	}
	return commandID, nil
}

// -- RuntimeBus 接口实现 --

// PrepareRun 创建 task + run 的最小初始状态（仅 task.created 事件），
// 构造并存储 trace_id，然后通过 Redis transport 入队 run job。
//
// 流程：
//  1. 委托 InMemoryRuntimeBus.PrepareMinimalRun 创建最小初始状态
//     （不生成 simple_success mock 事件，只生成 task.created）
//  2. 生成 trace_id 并存入内部 map（供后续 ResolvePermission 复用）
//  3. 构造 RunJobMessage（携带 job_id / trace_id / task_id / run_id 等）
//  4. 调用 transport.EnqueueRunJob 将 job 入队到 Redis StreamRunQueue
//  5. enqueue 成功 → 返回 task/run/events（仅 task.created）
//     enqueue 失败 → 返回 error
//
// 返回的 task / run / events 均为深拷贝，handler 可安全使用。
func (b *RedisRuntimeBus) PrepareRun(input contracts.CreateTaskInput) (*contracts.TaskDTO, *contracts.AgentRunDTO, []contracts.RuntimeEvent, error) {
	// 1. 委托 in-memory 创建最小初始状态（仅 task.created，无 worker 事件）
	task, run, events, err := b.inMemory.PrepareMinimalRun(input)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("bus: prepare run in-memory: %w", err)
	}

	// 2. 生成 trace_id 并存储（后续 PermissionDecisionCommand 复用）
	traceID := uuid.NewString()
	b.mu.Lock()
	b.traceIDs[run.ID] = traceID
	b.mu.Unlock()

	// 3. 构造 RunJobMessage 并入队
	jobMsg := runtimeredis.RunJobMessage{
		JobID:         uuid.NewString(),
		TraceID:       traceID,
		TaskID:        task.ID,
		RunID:         run.ID,
		UserGoal:      input.UserGoal,
		WorkspacePath: input.WorkspacePath,
		CreatedAt:     contracts.NowISO(),
		SchemaVersion: runtimeredis.SchemaVersion,
	}

	if err := b.transport.EnqueueRunJob(context.Background(), jobMsg); err != nil {
		return nil, nil, nil, fmt.Errorf("bus: prepare run enqueue: %w", err)
	}

	return task, run, events, nil
}

// SeedAcceptedRun 把 Python Control Plane 返回的权威 Task/Run/Event 写入
// Gateway 的实时内存投影。该方法不生成 ID、不写 PostgreSQL、也不向 Redis 入队。
// EventPump 应在 Worker 事件到达前看到该 Run；若发生竞态，事件会保留在 PEL
// 并由可靠性接管重试，不能因 run not found 直接丢失。
func (b *RedisRuntimeBus) SeedAcceptedRun(
	task contracts.TaskDTO,
	run contracts.AgentRunDTO,
	events []contracts.RuntimeEvent,
) {
	b.inMemory.SeedAcceptedRun(task, run, events)
}

// SetProjectionLoader allows background-created Runs (for example schedules)
// to be verified against PostgreSQL and adopted by the realtime projection.
func (b *RedisRuntimeBus) SetProjectionLoader(loader RuntimeProjectionLoader) {
	if b.eventPump != nil {
		b.eventPump.setProjectionLoader(loader)
	}
}

// GetEvents 返回 run 当前所有已产生事件的深拷贝。
//
// 委托 InMemoryRuntimeBus.GetEvents，不直接读取 Redis Stream。
// redis 模式下，EventPump 已通过 RuntimeEventReader 从 Redis runtime event stream
// 读取 worker 事件并追加到 InMemoryRuntimeBus（AppendRuntimeEvents），
// 因此 handler/SSE 通过本方法可读到 worker 产生的事件。
// Storage 落地后由 Storage-backed StateStore 替代 in-memory 临时 state owner。
//
// 返回的事件为深拷贝，handler 可安全使用。
func (b *RedisRuntimeBus) GetEvents(runID contracts.ID) ([]contracts.RuntimeEvent, error) {
	return b.inMemory.GetEvents(runID)
}

// ResolvePermission 处理权限决策，使用原子 reserve → publish → ack/restore 流程。
//
// 流程：
//  1. InMemoryRuntimeBus.ReservePermissionRequest 原子占用 pending permission
//     （写锁内查找+删除+深拷贝）。并发重复请求只有一个能 reserve 成功。
//  2. 从内部 traceIDs 中查找该 run 的 trace_id
//  3. 构造 PermissionDecisionCommand（trace_id 与 RunJobMessage 一致）
//  4. transport.PublishPermissionDecision 写入 Redis StreamWorkerCommand
//  5. publish 成功 → CommitPermissionDecisionAckFromReserved 提交 ack
//     （仅追加 permission.resolved，不生成 worker outcome）
//  6. publish 失败 → RestorePermissionRequest 恢复 pending → 返回 error，允许重试
//
// 并发安全：
//   - ReservePermissionRequest 是原子操作，两个并发请求中只有一个能成功 reserve
//   - reserve 失败直接返回 error，不触发 publish，避免重复 permission decision command
//   - publish 失败会 restore pending，可重试
//
// 约束：
//   - 不生成 tool.call.finished/failed、agent.step.completed、agent.run.completed
//   - 工具执行结果、step/run 完成事件后续必须由 Python worker 通过 RuntimeEvent 写入
//   - 不更新 run 状态为 completed
//
// 返回：reserved permReq（深拷贝）+ events（仅 permission.resolved 确认事件）
func (b *RedisRuntimeBus) ResolvePermission(decision contracts.PermissionDecisionDTO) (*contracts.PermissionRequestDTO, []contracts.RuntimeEvent, error) {
	// 1. 原子占用 pending permission（并发安全）
	permReq, ok := b.inMemory.ReservePermissionRequest(decision.RequestID)
	if !ok {
		return nil, nil, fmt.Errorf("bus: permission request not found or already reserved: %s", decision.RequestID)
	}

	// 2. 查找该 run 的 trace_id（与 PrepareRun 时生成的 trace_id 一致）
	b.mu.RLock()
	traceID, hasTrace := b.traceIDs[permReq.RunID]
	b.mu.RUnlock()
	if !hasTrace {
		// 如果 trace_id 不存在（如 dev mock 路径），生成新的并存储
		traceID = uuid.NewString()
		b.mu.Lock()
		b.traceIDs[permReq.RunID] = traceID
		b.mu.Unlock()
	}

	// 3. 构造 PermissionDecisionCommand
	cmd := runtimeredis.PermissionDecisionCommand{
		CommandID:     uuid.NewString(),
		TraceID:       traceID,
		RequestID:     decision.RequestID,
		TaskID:        permReq.TaskID,
		RunID:         permReq.RunID,
		Decision:      decision.Decision,
		Note:          decision.Note,
		DecidedAt:     contracts.NowISO(),
		SchemaVersion: runtimeredis.SchemaVersion,
	}

	// 4. publish 到 Redis
	if err := b.transport.PublishPermissionDecision(context.Background(), cmd); err != nil {
		// publish 失败 → 恢复 pending，允许重试
		b.inMemory.RestorePermissionRequest(permReq)
		return nil, nil, fmt.Errorf("bus: resolve permission publish: %w", err)
	}

	// 5. publish 成功 → 不生成 Go 侧 permission.resolved
	//    worker 收到 permission.decision 后自行生成 permission.resolved，
	//    避免双重 resolved 导致前端重复事件
	return permReq, []contracts.RuntimeEvent{}, nil
}

// -- RuntimeStateStore 接口实现（全部委托 in-memory） --

// GetRun 返回 run 的值拷贝；ok=false 表示不存在。
func (b *RedisRuntimeBus) GetRun(runID contracts.ID) (*contracts.AgentRunDTO, bool) {
	return b.inMemory.GetRun(runID)
}

// UpdateRunStatus 更新 run 状态和更新时间。
func (b *RedisRuntimeBus) UpdateRunStatus(runID contracts.ID, status string) {
	b.inMemory.UpdateRunStatus(runID, status)
}

// GetTask 返回 task 的值拷贝；ok=false 表示不存在。
func (b *RedisRuntimeBus) GetTask(taskID contracts.ID) (*contracts.TaskDTO, bool) {
	return b.inMemory.GetTask(taskID)
}

// ListTasks 返回所有 task 的值拷贝切片。
func (b *RedisRuntimeBus) ListTasks() []contracts.TaskDTO {
	return b.inMemory.ListTasks()
}

// -- PumpCloser 接口实现 --

// Start 启动 event pump 和 heartbeat pump（仅 redis 模式下有效）。
//
// 若 pump 为 nil（inmemory 模式），直接返回 nil（无操作）。
// 否则委托 pump.Start()：创建 consumer group 并启动后台泵循环。
// heartbeat pump 启动失败不影响 event pump 已启动状态。
func (b *RedisRuntimeBus) Start() error {
	if b.eventPump != nil {
		if err := b.eventPump.Start(); err != nil {
			return err
		}
	}
	if b.heartbeatPump != nil {
		if err := b.heartbeatPump.Start(); err != nil {
			return err
		}
	}
	return nil
}

// Close 停止 event pump 和 heartbeat pump。
//
// 若 pump 为 nil（inmemory 模式），直接返回 nil（无操作）。
// 先关 heartbeat pump，再关 event pump。
func (b *RedisRuntimeBus) Close() error {
	if b.heartbeatPump != nil {
		if err := b.heartbeatPump.Close(); err != nil {
			return err
		}
	}
	if b.eventPump != nil {
		return b.eventPump.Close()
	}
	return nil
}

// CancelRun 实现 RuntimeBus.CancelRun。
//
// Redis 模式（3C cancel）：
//  1. 查找 run 的 trace_id（与 PrepareRun 时一致）
//  2. 构造 RunCancelCommand
//  3. transport.PublishRunCancel 写入 Redis StreamWorkerCommand
//  4. 由 Python worker 消费命令并发出 agent.run.cancelled RuntimeEvent
//
// Gateway 不直接生成 agent.run.cancelled，不直接更新 run 状态。
// run 不存在时返回 error。
// trace_id 不存在时（如 dev mock 路径）生成新的并存储。
func (b *RedisRuntimeBus) CancelRun(runID contracts.ID) (*contracts.AgentRunDTO, error) {
	run, ok := b.inMemory.GetRun(runID)
	if !ok {
		return nil, fmt.Errorf("bus: run not found: %s", runID)
	}

	// 查找 trace_id
	b.mu.RLock()
	traceID, hasTrace := b.traceIDs[runID]
	b.mu.RUnlock()
	if !hasTrace {
		traceID = uuid.NewString()
		b.mu.Lock()
		b.traceIDs[runID] = traceID
		b.mu.Unlock()
	}

	cmd := runtimeredis.RunCancelCommand{
		CommandID:     uuid.NewString(),
		TraceID:       traceID,
		TaskID:        run.TaskID,
		RunID:         runID,
		Type:          "run.cancel",
		RequestedAt:   contracts.NowISO(),
		Reason:        "",
		SchemaVersion: runtimeredis.SchemaVersion,
	}

	if err := b.transport.PublishRunCancel(context.Background(), cmd); err != nil {
		return nil, fmt.Errorf("bus: cancel run publish: %w", err)
	}

	// 不更新本地 run 状态，不生成 cancelled 事件 —— 由 worker 负责
	return run, nil
}

// GetWorkerStatuses 返回所有已知 worker 状态列表（3B heartbeat）。
//
// 委托 WorkerStatusView.GetAll，返回深拷贝。
// inmemory 模式下无心跳数据，返回空列表。
func (b *RedisRuntimeBus) GetWorkerStatuses() []WorkerStatus {
	return b.workerStatusView.GetAll()
}

// SetRuntimeDiagnosticsReader 注入只读 Redis 诊断适配器。
func (b *RedisRuntimeBus) SetRuntimeDiagnosticsReader(reader runtimeredis.RuntimeDiagnosticsReader) {
	b.diagnostics = reader
}

type RuntimeHealthCounters struct {
	RunReclaimed        int64 `json:"run_reclaimed"`
	RunRetryDeferred    int64 `json:"run_retry_deferred"`
	RunDeadLettered     int64 `json:"run_dead_lettered"`
	RunMalformed        int64 `json:"run_malformed"`
	CommandReclaimed    int64 `json:"command_reclaimed"`
	CommandDeadLettered int64 `json:"command_dead_lettered"`
	CommandMalformed    int64 `json:"command_malformed"`
	EventReclaimed      int64 `json:"event_reclaimed"`
	EventRetryDeferred  int64 `json:"event_retry_deferred"`
	EventDeadLettered   int64 `json:"event_dead_lettered"`
	EventMalformed      int64 `json:"event_malformed"`
}

type RuntimeWorkerSummary struct {
	Total  int `json:"total"`
	Online int `json:"online"`
	Busy   int `json:"busy"`
	Stale  int `json:"stale"`
}

type RuntimeHealth struct {
	Status      string                               `json:"status"`
	RuntimeBus  string                               `json:"runtime_bus"`
	GeneratedAt string                               `json:"generated_at"`
	Workers     RuntimeWorkerSummary                 `json:"workers"`
	Streams     []runtimeredis.StreamDiagnostics     `json:"streams"`
	DeadLetters []runtimeredis.DeadLetterDiagnostics `json:"dead_letters"`
	Counters    RuntimeHealthCounters                `json:"counters"`
	Warnings    []string                             `json:"warnings"`
}

// GetRuntimeHealth 聚合 heartbeat、Redis consumer group 与 DLQ 元数据。
// Redis 消息 payload 不进入该投影。
func (b *RedisRuntimeBus) GetRuntimeHealth(ctx context.Context) RuntimeHealth {
	health := RuntimeHealth{
		Status: "healthy", RuntimeBus: "redis", GeneratedAt: time.Now().UTC().Format(time.RFC3339Nano),
		Streams: []runtimeredis.StreamDiagnostics{}, DeadLetters: []runtimeredis.DeadLetterDiagnostics{}, Warnings: []string{},
	}
	workers := b.GetWorkerStatuses()
	health.Workers.Total = len(workers)
	for _, worker := range workers {
		if worker.IsStale {
			health.Workers.Stale++
		} else {
			health.Workers.Online++
		}
		if worker.Status == "busy" && !worker.IsStale {
			health.Workers.Busy++
		}
		if worker.RuntimeBus != nil {
			health.Counters.RunReclaimed += worker.RuntimeBus.Reclaimed
			health.Counters.RunRetryDeferred += worker.RuntimeBus.RetryDeferred
			health.Counters.RunDeadLettered += worker.RuntimeBus.DeadLettered
			health.Counters.RunMalformed += worker.RuntimeBus.Malformed
			health.Counters.CommandReclaimed += worker.RuntimeBus.CommandReclaimed
			health.Counters.CommandDeadLettered += worker.RuntimeBus.CommandDeadLettered
			health.Counters.CommandMalformed += worker.RuntimeBus.CommandMalformed
		}
	}
	if b.eventPump != nil {
		metrics := b.eventPump.metricsSnapshot()
		health.Counters.EventReclaimed = metrics.reclaimed
		health.Counters.EventRetryDeferred = metrics.retryDeferred
		health.Counters.EventDeadLettered = metrics.deadLettered
		health.Counters.EventMalformed = metrics.malformed
	}
	if b.diagnostics == nil {
		health.Status = "unavailable"
		health.Warnings = append(health.Warnings, "Redis 运行时诊断不可用")
		return health
	}
	for _, target := range []struct{ name, stream, group string }{
		{"run_queue", runtimeredis.StreamRunQueue, runtimeredis.GroupWorkerPool},
		{"worker_command", runtimeredis.StreamWorkerCommand, runtimeredis.GroupWorkerPool},
		{"runtime_event", runtimeredis.StreamRuntimeEvent, runtimeredis.GroupGatewayEvents},
	} {
		stream := b.diagnostics.InspectGroup(ctx, target.name, target.stream, target.group)
		health.Streams = append(health.Streams, stream)
		if !stream.Available || stream.Pending > 0 || stream.Lag != 0 {
			health.Status = "degraded"
		}
	}
	for _, target := range []struct{ name, stream string }{
		{"run_queue", runtimeredis.StreamRunDeadLetter},
		{"worker_command", runtimeredis.StreamWorkerCommandDeadLetter},
		{"runtime_event", runtimeredis.StreamRuntimeEventDeadLetter},
	} {
		dlq, err := b.diagnostics.DeadLetterLength(ctx, target.name, target.stream)
		if err != nil {
			health.Status = "degraded"
			health.Warnings = append(health.Warnings, "部分 DLQ 统计暂不可用")
			continue
		}
		health.DeadLetters = append(health.DeadLetters, dlq)
	}
	if health.Workers.Online == 0 {
		health.Status = "degraded"
		health.Warnings = append(health.Warnings, "没有在线 Worker")
	}
	return health
}

// ListRuntimeDeadLetters 查询一个 DLQ 的安全白名单投影。
func (b *RedisRuntimeBus) ListRuntimeDeadLetters(
	ctx context.Context, source string, limit int, before, errorCode, taskID, runID string,
) (runtimeredis.DeadLetterPage, error) {
	if b.diagnostics == nil {
		return runtimeredis.DeadLetterPage{}, fmt.Errorf("bus: Redis 运行时诊断不可用")
	}
	streams := map[string]string{
		"run_queue":      runtimeredis.StreamRunDeadLetter,
		"worker_command": runtimeredis.StreamWorkerCommandDeadLetter,
		"runtime_event":  runtimeredis.StreamRuntimeEventDeadLetter,
	}
	stream, ok := streams[source]
	if !ok {
		return runtimeredis.DeadLetterPage{}, fmt.Errorf("bus: 不支持的 DLQ source: %s", source)
	}
	return b.diagnostics.ListDeadLetters(ctx, runtimeredis.DeadLetterQuery{
		Name: source, Stream: stream, Limit: limit, Before: before,
		ErrorCode: errorCode, TaskID: taskID, RunID: runID,
	})
}

// GetRuntimeDeadLetter 按固定 source 映射精确读取一条 DLQ 白名单记录。
func (b *RedisRuntimeBus) GetRuntimeDeadLetter(
	ctx context.Context, source, id string,
) (*runtimeredis.DeadLetterRecord, error) {
	if b.diagnostics == nil {
		return nil, fmt.Errorf("bus: Redis 运行时诊断不可用")
	}
	streams := map[string]string{
		"run_queue":      runtimeredis.StreamRunDeadLetter,
		"worker_command": runtimeredis.StreamWorkerCommandDeadLetter,
		"runtime_event":  runtimeredis.StreamRuntimeEventDeadLetter,
	}
	stream, ok := streams[source]
	if !ok {
		return nil, fmt.Errorf("bus: 不支持的 DLQ source: %s", source)
	}
	return b.diagnostics.GetDeadLetter(ctx, source, stream, id)
}

// -- 编译期断言 --

// 确保 RedisRuntimeBus 实现 PumpCloser 接口。
var _ PumpCloser = (*RedisRuntimeBus)(nil)
