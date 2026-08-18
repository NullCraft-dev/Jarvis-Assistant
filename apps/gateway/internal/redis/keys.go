package redis

// Redis Stream Key 命名约定：
//
//	jarvis:stream:<用途>
//
// 所有 key 使用小写 + 连字符，保持稳定、可测试、可文档化。
// 这些 key 用于 Redis Streams / consumer group，不承载业务数据真源。

const (
	// StreamRunQueue 是 Go Orchestrator 向 Python worker pool 下发 run job 的 stream。
	// 生产者：Go Orchestrator（RunScheduler）
	// 消费者：Python Agent Worker Pool（consumer group）
	StreamRunQueue = "jarvis:stream:run-queue"

	// StreamRunDeadLetter 保存达到最大投递次数或确定性格式错误的 RunJob。
	// 它是运行时诊断记录，不替代 PostgreSQL Task/Run/AuditLog 真源。
	StreamRunDeadLetter = "jarvis:stream:run-dead-letter"

	// StreamWorkerCommandDeadLetter 保存非法 worker command 的脱敏诊断副本。
	StreamWorkerCommandDeadLetter = "jarvis:stream:worker-command-dead-letter"

	// StreamWorkerCommand 是 Go Orchestrator 向 Python worker 下发运行时命令的 stream。
	// 命令类型：pause / resume / cancel / retry_step / permission.resolve / worker.shutdown
	// 生产者：Go Orchestrator（RunScheduler）
	// 消费者：Python Agent Worker（按 worker_id 路由或 consumer group）
	StreamWorkerCommand = "jarvis:stream:worker-command"

	// StreamRuntimeEvent 是 Python worker 向 Go Orchestrator 上报 RuntimeEvent 的 stream。
	// 生产者：Python Agent Worker
	// 消费者：Go Orchestrator（EventStreamProxy / fan-out）
	StreamRuntimeEvent = "jarvis:stream:runtime-event"

	// StreamRuntimeEventDeadLetter 保存无法投影的 runtime event 脱敏诊断副本。
	// 权威 RuntimeEvent 仍以 PostgreSQL 为准。
	StreamRuntimeEventDeadLetter = "jarvis:stream:runtime-event-dead-letter"

	// StreamWorkerHeartbeat 是 Python worker 向 Go Orchestrator 上报心跳和状态的 stream。
	// 生产者：Python Agent Worker
	// 消费者：Go Orchestrator（WorkerManager）
	StreamWorkerHeartbeat = "jarvis:stream:worker-heartbeat"

	// StreamPendingPermission 是 worker 向 Go Orchestrator 发送待处理权限请求的 stream。
	// 用于 worker 需要用户确认时，将权限请求通知 Go 侧并等待 decision command 回传。
	// 生产者：Python Agent Worker
	// 消费者：Go Orchestrator
	StreamPendingPermission = "jarvis:stream:pending-permission"
)

// Consumer Group 命名约定：
//
//	jarvis:group:<用途>
const (
	// GroupWorkerPool 是 Python worker pool 的 consumer group 名称。
	// 多个 worker 实例使用同一 consumer group，Redis 自动负载均衡。
	GroupWorkerPool = "jarvis:group:worker-pool"

	// GroupGatewayEvents 是 Go Gateway 消费 runtime event stream 的 consumer group。
	GroupGatewayEvents = "jarvis:group:gateway-events"
)

// Redis Stream message field 命名。
// 这些常量用于 message 在 Redis Stream 中的 field name（XADD key-value 对）。
// 当前仅作为契约文档；2B-1b 接入 go-redis 时直接使用。
const (
	// FieldJobID 是 run job 的 job_id 字段名。
	FieldJobID = "job_id"
	// FieldTaskID 是 task id 字段名。
	FieldTaskID = "task_id"
	// FieldRunID 是 run id 字段名。
	FieldRunID = "run_id"
	// FieldEventID 是 event id 字段名。
	FieldEventID = "event_id"
	// FieldCommandID 是 command id 字段名。
	FieldCommandID = "command_id"
	// FieldRequestID 是 permission request id 字段名。
	FieldRequestID = "request_id"
	// FieldWorkerID 是 worker id 字段名。
	FieldWorkerID = "worker_id"
	// FieldSchemaVersion 是 schema version 字段名。
	FieldSchemaVersion = "schema_version"
	// FieldTraceID 是链路追踪 id 字段名。
	// 所有 command / event 消息必须携带；heartbeat 不强制。
	FieldTraceID = "trace_id"
	// FieldPayload 是 message payload 字段名（JSON 编码）。
	FieldPayload = "payload"
)

// SchemaVersion 是当前 Redis message contract 的版本号。
// 所有消息必须携带此版本号；consumer 应校验版本兼容性。
const SchemaVersion = "2B-1a.1"
