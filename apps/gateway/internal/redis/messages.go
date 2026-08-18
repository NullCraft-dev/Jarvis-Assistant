package redis

import (
	"encoding/json"
	"fmt"

	"github.com/jarvis-assistant/gateway/internal/contracts"
)

// RunJobMessage 是 Go Orchestrator 入队到 Redis run queue 的 job 消息。
// Python worker 消费此消息后启动 AgentRun loop。
// 作为 command/event 链路起点，必须携带 TraceID。
type RunJobMessage struct {
	// JobID 是本次入队的唯一标识，用于幂等和追踪。
	JobID contracts.ID `json:"job_id"`
	// TraceID 是链路追踪 id，贯穿此次 run 的所有 command / event。
	TraceID contracts.ID `json:"trace_id"`
	// TaskID 是关联的 Task id。
	TaskID contracts.ID `json:"task_id"`
	// RunID 是关联的 AgentRun id。
	RunID contracts.ID `json:"run_id"`
	// UserGoal 是用户输入的任务目标原文。
	UserGoal string `json:"user_goal"`
	// WorkspacePath 是任务工作区路径，可选。
	WorkspacePath string `json:"workspace_path,omitempty"`
	// CreatedAt 是 job 入队时间（ISO 8601）。
	CreatedAt string `json:"created_at"`
	// SchemaVersion 是消息契约版本，必须为 SchemaVersion 常量值。
	SchemaVersion string `json:"schema_version"`
}

// RuntimeEventEnvelope 是 Python worker 通过 Redis 上报 RuntimeEvent 的传输信封。
// 它在 contracts.RuntimeEvent 之上附加了传输层元数据，不重新定义 RuntimeEvent 的 shape。
// 作为 event 消息，必须携带 TraceID。
type RuntimeEventEnvelope struct {
	// EventID 是本次 envelope 的唯一标识。
	EventID contracts.ID `json:"event_id"`
	// TraceID 是链路追踪 id，与对应 run job 的 trace_id 一致。
	TraceID contracts.ID `json:"trace_id"`
	// TaskID 是关联的 Task id。
	TaskID contracts.ID `json:"task_id"`
	// RunID 是关联的 AgentRun id。
	RunID contracts.ID `json:"run_id"`
	// EventType 是事件类型字符串，与内层 RuntimeEvent.Type 一致（冗余字段，方便 consumer 路由）。
	EventType string `json:"event_type"`
	// RuntimeEvent 是实际的 RuntimeEvent，其 shape 由 contracts.RuntimeEvent 定义。
	// 禁止在 envelope 层重新定义 RuntimeEvent 的字段。
	RuntimeEvent contracts.RuntimeEvent `json:"runtime_event"`
	// ProducedBy 标识产生此事件的 worker id。
	ProducedBy string `json:"produced_by"`
	// SchemaVersion 是消息契约版本，必须为 SchemaVersion 常量值。
	SchemaVersion string `json:"schema_version"`
}

// PermissionDecisionCommand 是 Go Orchestrator 通过 worker command stream
// 发送给 Python worker 的权限决策命令。
// 作为 command 消息，必须携带 TraceID。
type PermissionDecisionCommand struct {
	// CommandID 是本次命令的唯一标识。
	CommandID contracts.ID `json:"command_id"`
	// TraceID 是链路追踪 id，与对应 run job 的 trace_id 一致。
	TraceID contracts.ID `json:"trace_id"`
	// RequestID 是被决议的权限请求 id，对应 contracts.PermissionRequestDTO.ID。
	RequestID contracts.ID `json:"request_id"`
	// TaskID 是关联的 Task id。
	TaskID contracts.ID `json:"task_id"`
	// RunID 是关联的 AgentRun id。
	RunID contracts.ID `json:"run_id"`
	// Decision 是用户做出的权限决策，类型复用 contracts.PermissionDecisionType。
	Decision contracts.PermissionDecisionType `json:"decision"`
	// Note 是用户可选备注。
	Note string `json:"note,omitempty"`
	// DecidedAt 是用户做出决策的时间（ISO 8601）。
	DecidedAt string `json:"decided_at"`
	// SchemaVersion 是消息契约版本，必须为 SchemaVersion 常量值。
	SchemaVersion string `json:"schema_version"`
}

// RunCancelCommand 是 Go Orchestrator 通过 worker command stream
// 发送给 Python worker 的取消运行命令（3C cancel）。
//
// 作为 command 消息，必须携带 TraceID。
type RunCancelCommand struct {
	// CommandID 是本次命令的唯一标识。
	CommandID contracts.ID `json:"command_id"`
	// TraceID 是链路追踪 id，与对应 run job 的 trace_id 一致。
	TraceID contracts.ID `json:"trace_id"`
	// TaskID 是关联的 Task id。
	TaskID contracts.ID `json:"task_id"`
	// RunID 是要取消的 AgentRun id。
	RunID contracts.ID `json:"run_id"`
	// Type 是命令类型，固定为 "run.cancel"。
	Type string `json:"type"`
	// RequestedAt 是命令发布时间（ISO 8601）。
	RequestedAt string `json:"requested_at"`
	// Reason 是可选取消原因。
	Reason string `json:"reason,omitempty"`
	// SchemaVersion 是消息契约版本，必须为 SchemaVersion 常量值。
	SchemaVersion string `json:"schema_version"`
}

// McpDiscoveryRefreshCommand 是 Gateway 发给任意空闲 Worker 的 MCP 管理命令。
// 它不携带 server 配置或密钥，Worker 必须从 Storage 读取权威配置。
type McpDiscoveryRefreshCommand struct {
	CommandID     contracts.ID `json:"command_id"`
	TraceID       contracts.ID `json:"trace_id"`
	Type          string       `json:"type"`
	RequestedAt   string       `json:"requested_at"`
	SchemaVersion string       `json:"schema_version"`
}

// ValidWorkerCommandTypes 是合法的 worker command 类型集合（3C）。
// 当前只支持 run.cancel。
var ValidWorkerCommandTypes = map[string]bool{
	"run.cancel":            true,
	"mcp.discovery.refresh": true,
}

// WorkerHeartbeatMessage 是 Python worker 通过 Redis 上报的心跳和状态消息。
//
// 心跳是状态探针，不属于 command / event 链路，因此不携带 trace_id。
// WorkerHeartbeatMessage 不需要 TraceID 字段。
type WorkerHeartbeatMessage struct {
	// WorkerID 是发送心跳的 worker 唯一标识。
	WorkerID contracts.ID `json:"worker_id"`
	// WorkerKind 区分执行 AgentRun 的 agent worker 与执行持久化 RAG 作业的 rag worker。
	// 旧消息缺省为 agent，以保持向后兼容。
	WorkerKind string `json:"worker_kind,omitempty"`
	// Status 是 worker 当前状态：starting / idle / busy / draining / stopped / failed。
	Status string `json:"status"`
	// ActiveRunID 是 worker 当前正在执行的 run id，idle 时为空。
	ActiveRunID contracts.ID `json:"active_run_id,omitempty"`
	// ReportedAt 是心跳上报时间（ISO 8601）。
	ReportedAt string `json:"reported_at"`
	// SchemaVersion 是消息契约版本，必须为 SchemaVersion 常量值。
	SchemaVersion string `json:"schema_version"`
	// Model 是模型配置状态（Phase 6B-1），可选字段兼容旧 heartbeat。
	Model *WorkerModelStatus `json:"model,omitempty"`
	// RuntimeBus 是 Redis Runtime Bus 进程级累计指标，可选字段兼容旧 heartbeat。
	RuntimeBus *WorkerRuntimeBusMetrics `json:"runtime_bus,omitempty"`
}

// WorkerModelStatus 是 heartbeat 中的模型配置状态（Phase 6B-1）。
type WorkerModelStatus struct {
	Provider         string  `json:"provider"`
	Protocol         string  `json:"protocol"`
	ModelName        string  `json:"model_name"`
	APIKeyConfigured bool    `json:"api_key_configured"`
	ThinkingMode     string  `json:"thinking_mode"`
	Status           string  `json:"status"`
	LastErrorCode    *string `json:"last_error_code"`
}

// WorkerRuntimeBusMetrics 是单个 Worker 进程自启动以来的 Run Queue 累计指标。
// 这些指标用于可观察性，不是业务真源，也不参与任务状态恢复。
type WorkerRuntimeBusMetrics struct {
	Reclaimed           int64 `json:"reclaimed"`
	RetryDeferred       int64 `json:"retry_deferred"`
	DeadLettered        int64 `json:"dead_lettered"`
	Malformed           int64 `json:"malformed"`
	CommandReclaimed    int64 `json:"command_reclaimed"`
	CommandDeadLettered int64 `json:"command_dead_lettered"`
	CommandMalformed    int64 `json:"command_malformed"`
}

// -- 序列化 helper --

// MarshalJSON 已由 struct tag 自动支持，以下提供 map-based helper
// 方便后续接入 Redis XADD（field-value pairs）时使用。

// ToMap 将消息序列化为 map[string]interface{}。
// 内部先 JSON marshal，再 unmarshal 到 map，适合契约测试和 Redis field-value 转换。
func ToMap(v interface{}) (map[string]interface{}, error) {
	data, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("redisruntime: marshal: %w", err)
	}
	var m map[string]interface{}
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("redisruntime: unmarshal to map: %w", err)
	}
	return m, nil
}

// FromMap 从 map[string]interface{} 反序列化到目标 struct。
// 内部先 JSON marshal map，再 unmarshal 到 struct。
// 注意：FromMap 只做 JSON 往返，不做字段校验；使用 Decode* 函数进行类型化校验。
func FromMap(m map[string]interface{}, v interface{}) error {
	data, err := json.Marshal(m)
	if err != nil {
		return fmt.Errorf("redisruntime: marshal map: %w", err)
	}
	if err := json.Unmarshal(data, v); err != nil {
		return fmt.Errorf("redisruntime: unmarshal from map: %w", err)
	}
	return nil
}

// ValidateSchemaVersion 校验消息的 schema_version 字段是否存在、为 string 且与当前版本精确匹配。
// 返回 nil 表示版本匹配；否则返回描述性 error。
func ValidateSchemaVersion(m map[string]interface{}) error {
	ver, ok := m[FieldSchemaVersion]
	if !ok {
		return fmt.Errorf("redisruntime: missing field %s", FieldSchemaVersion)
	}
	s, ok := ver.(string)
	if !ok {
		return fmt.Errorf("redisruntime: field %s is not a string, got %T", FieldSchemaVersion, ver)
	}
	if s == "" {
		return fmt.Errorf("redisruntime: field %s is empty", FieldSchemaVersion)
	}
	if s != SchemaVersion {
		return fmt.Errorf("redisruntime: schema_version mismatch: got %q, want %q", s, SchemaVersion)
	}
	return nil
}

// ValidateRequiredFields 校验 map 中存在所有指定的必要字段，且字符串字段非空。
// 用于反序列化后检查关键字段不缺失。
func ValidateRequiredFields(m map[string]interface{}, fields ...string) error {
	for _, f := range fields {
		v, ok := m[f]
		if !ok {
			return fmt.Errorf("redisruntime: missing required field %s", f)
		}
		if v == nil {
			return fmt.Errorf("redisruntime: required field %s is nil", f)
		}
		// 字符串字段额外检查非空
		if s, ok := v.(string); ok && s == "" {
			return fmt.Errorf("redisruntime: required field %s is empty", f)
		}
	}
	return nil
}

// -- 类型化 Decode 函数 --

// runJobRequiredFields 是 RunJobMessage 必须非空的字段。
var runJobRequiredFields = []string{FieldJobID, FieldTraceID, FieldTaskID, FieldRunID, "user_goal", "created_at", FieldSchemaVersion}

// DecodeRunJobMessage 从 map 反序列化并校验 RunJobMessage。
// 校验：schema_version 精确匹配、必要字段非空。
func DecodeRunJobMessage(m map[string]interface{}) (RunJobMessage, error) {
	if err := ValidateSchemaVersion(m); err != nil {
		return RunJobMessage{}, err
	}
	if err := ValidateRequiredFields(m, runJobRequiredFields...); err != nil {
		return RunJobMessage{}, err
	}
	var msg RunJobMessage
	if err := FromMap(m, &msg); err != nil {
		return RunJobMessage{}, err
	}
	return msg, nil
}

// DecodeRuntimeEventEnvelope 从 map 反序列化并校验 RuntimeEventEnvelope。
// 校验：
//   - schema_version 精确匹配
//   - envelope 层必要字段（event_id / trace_id / task_id / run_id / event_type / produced_by）非空
//   - 内层 runtime_event.id / type / task_id / run_id / timestamp 非空
//   - envelope.event_type 与 runtime_event.type 一致
//   - envelope.task_id / run_id 与 runtime_event.task_id / run_id 一致
func DecodeRuntimeEventEnvelope(m map[string]interface{}) (RuntimeEventEnvelope, error) {
	if err := ValidateSchemaVersion(m); err != nil {
		return RuntimeEventEnvelope{}, err
	}
	if err := ValidateRequiredFields(m, FieldEventID, FieldTraceID, FieldTaskID, FieldRunID, "event_type", "produced_by", FieldSchemaVersion); err != nil {
		return RuntimeEventEnvelope{}, err
	}

	var env RuntimeEventEnvelope
	if err := FromMap(m, &env); err != nil {
		return RuntimeEventEnvelope{}, err
	}

	// 校验内层 RuntimeEvent 核心字段
	re := env.RuntimeEvent
	if re.ID == "" {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: runtime_event.id is empty")
	}
	if re.Type == "" {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: runtime_event.type is empty")
	}
	if re.TaskID == "" {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: runtime_event.task_id is empty")
	}
	if re.RunID == "" {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: runtime_event.run_id is empty")
	}
	if re.Timestamp == "" {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: runtime_event.timestamp is empty")
	}

	// envelope 与内层一致性
	if env.EventID != re.ID {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: event_id %q != runtime_event.id %q", env.EventID, re.ID)
	}
	if env.EventType != re.Type {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: event_type %q != runtime_event.type %q", env.EventType, re.Type)
	}
	if env.TaskID != re.TaskID {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: envelope.task_id %q != runtime_event.task_id %q", env.TaskID, re.TaskID)
	}
	if env.RunID != re.RunID {
		return RuntimeEventEnvelope{}, fmt.Errorf("redisruntime: envelope.run_id %q != runtime_event.run_id %q", env.RunID, re.RunID)
	}

	return env, nil
}

// permissionDecisionRequiredFields 是 PermissionDecisionCommand 必须非空的字段。
var permissionDecisionRequiredFields = []string{FieldCommandID, FieldTraceID, FieldRequestID, FieldTaskID, FieldRunID, "decision", "decided_at", FieldSchemaVersion}

// DecodePermissionDecisionCommand 从 map 反序列化并校验 PermissionDecisionCommand。
// 校验：schema_version 精确匹配、必要字段非空。
func DecodePermissionDecisionCommand(m map[string]interface{}) (PermissionDecisionCommand, error) {
	if err := ValidateSchemaVersion(m); err != nil {
		return PermissionDecisionCommand{}, err
	}
	if err := ValidateRequiredFields(m, permissionDecisionRequiredFields...); err != nil {
		return PermissionDecisionCommand{}, err
	}
	var msg PermissionDecisionCommand
	if err := FromMap(m, &msg); err != nil {
		return PermissionDecisionCommand{}, err
	}
	return msg, nil
}

// runCancelRequiredFields 是 RunCancelCommand 必须非空的字段。
var runCancelRequiredFields = []string{FieldCommandID, FieldTraceID, FieldTaskID, FieldRunID, "type", "requested_at", FieldSchemaVersion}

// DecodeRunCancelCommand 从 map 反序列化并校验 RunCancelCommand。
// 校验：schema_version 精确匹配、必要字段非空、type 为合法 command 类型。
func DecodeRunCancelCommand(m map[string]interface{}) (RunCancelCommand, error) {
	if err := ValidateSchemaVersion(m); err != nil {
		return RunCancelCommand{}, err
	}
	if err := ValidateRequiredFields(m, runCancelRequiredFields...); err != nil {
		return RunCancelCommand{}, err
	}
	var cmd RunCancelCommand
	if err := FromMap(m, &cmd); err != nil {
		return RunCancelCommand{}, err
	}

	// 校验 command type 合法性
	if !ValidWorkerCommandTypes[cmd.Type] {
		return RunCancelCommand{}, fmt.Errorf("redisruntime: unsupported command type %q, only run.cancel is supported", cmd.Type)
	}

	return cmd, nil
}

var mcpDiscoveryRefreshRequiredFields = []string{
	FieldCommandID, FieldTraceID, "type", "requested_at", FieldSchemaVersion,
}

func DecodeMcpDiscoveryRefreshCommand(m map[string]interface{}) (McpDiscoveryRefreshCommand, error) {
	if err := ValidateSchemaVersion(m); err != nil {
		return McpDiscoveryRefreshCommand{}, err
	}
	if err := ValidateRequiredFields(m, mcpDiscoveryRefreshRequiredFields...); err != nil {
		return McpDiscoveryRefreshCommand{}, err
	}
	var cmd McpDiscoveryRefreshCommand
	if err := FromMap(m, &cmd); err != nil {
		return McpDiscoveryRefreshCommand{}, err
	}
	if cmd.Type != "mcp.discovery.refresh" {
		return McpDiscoveryRefreshCommand{}, fmt.Errorf(
			"redisruntime: invalid MCP discovery command type %q", cmd.Type,
		)
	}
	return cmd, nil
}

// ValidWorkerStatuses 是合法的 worker status 枚举值集合。
var ValidWorkerStatuses = map[string]bool{
	"starting": true,
	"idle":     true,
	"busy":     true,
	"draining": true,
	"stopped":  true,
	"failed":   true,
}

// ValidWorkerKinds 是合法的 worker 类型。旧 heartbeat 缺省按 agent 处理。
var ValidWorkerKinds = map[string]bool{
	"agent": true,
	"rag":   true,
}

// heartbeatRequiredFields 是 WorkerHeartbeatMessage 必须非空的字段。
// 注意：不包含 trace_id，心跳不属于 command / event 链路。
var heartbeatRequiredFields = []string{FieldWorkerID, "status", "reported_at", FieldSchemaVersion}

// DecodeWorkerHeartbeatMessage 从 map 反序列化并校验 WorkerHeartbeatMessage。
// 校验：
//   - schema_version 精确匹配
//   - worker_id / status / reported_at 非空
//   - status 必须是合法枚举值（starting / idle / busy / draining / stopped / failed）
//   - agent worker 为 busy 时 active_run_id 必须非空
//   - rag worker 的工作单元是 RagIngestionJob，不伪造 AgentRun id
//
// 心跳是状态探针，不校验 trace_id。
func DecodeWorkerHeartbeatMessage(m map[string]interface{}) (WorkerHeartbeatMessage, error) {
	if err := ValidateSchemaVersion(m); err != nil {
		return WorkerHeartbeatMessage{}, err
	}
	if err := ValidateRequiredFields(m, heartbeatRequiredFields...); err != nil {
		return WorkerHeartbeatMessage{}, err
	}
	var msg WorkerHeartbeatMessage
	if err := FromMap(m, &msg); err != nil {
		return WorkerHeartbeatMessage{}, err
	}

	// 校验 status 枚举
	if !ValidWorkerStatuses[msg.Status] {
		return WorkerHeartbeatMessage{}, fmt.Errorf("redisruntime: invalid worker status %q, must be one of: starting | idle | busy | draining | stopped | failed", msg.Status)
	}
	if msg.WorkerKind == "" {
		msg.WorkerKind = "agent"
	}
	if !ValidWorkerKinds[msg.WorkerKind] {
		return WorkerHeartbeatMessage{}, fmt.Errorf("redisruntime: invalid worker kind %q, must be one of: agent | rag", msg.WorkerKind)
	}

	// 只有 agent worker 的 busy 状态绑定 AgentRun。
	if msg.WorkerKind == "agent" && msg.Status == "busy" && msg.ActiveRunID == "" {
		return WorkerHeartbeatMessage{}, fmt.Errorf("redisruntime: busy worker must have non-empty active_run_id")
	}

	return msg, nil
}
