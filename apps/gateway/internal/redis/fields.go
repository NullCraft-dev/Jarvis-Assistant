package redis

import (
	"encoding/json"
	"fmt"
)

// -- Stream field helper --

// ToStreamFields 系列函数将消息转换为 Redis XADD 的 field-value 对。
//
// 所有函数输出统一 shape：
//   - schema_version：当前契约版本号
//   - payload：完整 message 的 JSON 字符串（可 JSON decode 回原始 message）
//   - 冗余标量路由字段（来自同一 message struct，保证一致）
//
// nested object 不直接作为 Redis field value；所有嵌套结构都在 payload JSON 字符串中。

// -- RunJobMessage fields --

const streamTypeRunJob = "run.job"

// RunJobToStreamFields 将 RunJobMessage 转换为 XADD fields。
// 输出：schema_version / payload / job_id / trace_id / task_id / run_id / type / created_at
func RunJobToStreamFields(msg RunJobMessage) (map[string]interface{}, error) {
	payload, err := json.Marshal(msg)
	if err != nil {
		return nil, fmt.Errorf("redisruntime: marshal run job payload: %w", err)
	}
	return map[string]interface{}{
		FieldSchemaVersion: msg.SchemaVersion,
		FieldPayload:       string(payload),
		FieldJobID:         msg.JobID,
		FieldTraceID:       msg.TraceID,
		FieldTaskID:        msg.TaskID,
		FieldRunID:         msg.RunID,
		"type":             streamTypeRunJob,
		"created_at":       msg.CreatedAt,
	}, nil
}

// -- RuntimeEventEnvelope fields --

// RuntimeEventToStreamFields 将 RuntimeEventEnvelope 转换为 XADD fields。
// 输出：schema_version / payload / event_id / trace_id / task_id / run_id / type / produced_by
// type 字段等于 envelope.EventType，即 RuntimeEventType。
func RuntimeEventToStreamFields(env RuntimeEventEnvelope) (map[string]interface{}, error) {
	payload, err := json.Marshal(env)
	if err != nil {
		return nil, fmt.Errorf("redisruntime: marshal runtime event payload: %w", err)
	}
	return map[string]interface{}{
		FieldSchemaVersion: env.SchemaVersion,
		FieldPayload:       string(payload),
		FieldEventID:       env.EventID,
		FieldTraceID:       env.TraceID,
		FieldTaskID:        env.TaskID,
		FieldRunID:         env.RunID,
		"type":             env.EventType,
		"produced_by":      env.ProducedBy,
	}, nil
}

// -- PermissionDecisionCommand fields --

const streamTypePermissionDecision = "permission.decision"

// PermissionDecisionToStreamFields 将 PermissionDecisionCommand 转换为 XADD fields。
// 输出：schema_version / payload / command_id / trace_id / request_id / task_id / run_id / type / decided_at
func PermissionDecisionToStreamFields(cmd PermissionDecisionCommand) (map[string]interface{}, error) {
	payload, err := json.Marshal(cmd)
	if err != nil {
		return nil, fmt.Errorf("redisruntime: marshal permission decision payload: %w", err)
	}
	return map[string]interface{}{
		FieldSchemaVersion: cmd.SchemaVersion,
		FieldPayload:       string(payload),
		FieldCommandID:     cmd.CommandID,
		FieldTraceID:       cmd.TraceID,
		FieldRequestID:     cmd.RequestID,
		FieldTaskID:        cmd.TaskID,
		FieldRunID:         cmd.RunID,
		"type":             streamTypePermissionDecision,
		"decided_at":       cmd.DecidedAt,
	}, nil
}

// -- RunCancelCommand fields --

const streamTypeRunCancel = "run.cancel"

// RunCancelToStreamFields 将 RunCancelCommand 转换为 XADD fields。
// 输出：schema_version / payload / command_id / trace_id / task_id / run_id / type / requested_at
func RunCancelToStreamFields(cmd RunCancelCommand) (map[string]interface{}, error) {
	payload, err := json.Marshal(cmd)
	if err != nil {
		return nil, fmt.Errorf("redisruntime: marshal run cancel payload: %w", err)
	}
	return map[string]interface{}{
		FieldSchemaVersion: cmd.SchemaVersion,
		FieldPayload:       string(payload),
		FieldCommandID:     cmd.CommandID,
		FieldTraceID:       cmd.TraceID,
		FieldTaskID:        cmd.TaskID,
		FieldRunID:         cmd.RunID,
		"type":             streamTypeRunCancel,
		"requested_at":     cmd.RequestedAt,
	}, nil
}

// McpDiscoveryRefreshToStreamFields 将全局 MCP discovery 管理命令转换为
// worker-command stream fields。该命令不关联 Task/Run。
func McpDiscoveryRefreshToStreamFields(cmd McpDiscoveryRefreshCommand) (map[string]interface{}, error) {
	payload, err := json.Marshal(cmd)
	if err != nil {
		return nil, fmt.Errorf("redisruntime: marshal MCP discovery payload: %w", err)
	}
	return map[string]interface{}{
		FieldSchemaVersion: cmd.SchemaVersion,
		FieldPayload:       string(payload),
		FieldCommandID:     cmd.CommandID,
		FieldTraceID:       cmd.TraceID,
		"type":             cmd.Type,
		"requested_at":     cmd.RequestedAt,
	}, nil
}

// -- WorkerHeartbeatMessage fields --

const streamTypeWorkerHeartbeat = "worker.heartbeat"

// WorkerHeartbeatToStreamFields 将 WorkerHeartbeatMessage 转换为 XADD fields。
//
// 心跳是状态探针，不包含 trace_id。
// 输出：schema_version / payload / worker_id / type / status / reported_at
func WorkerHeartbeatToStreamFields(hb WorkerHeartbeatMessage) (map[string]interface{}, error) {
	payload, err := json.Marshal(hb)
	if err != nil {
		return nil, fmt.Errorf("redisruntime: marshal worker heartbeat payload: %w", err)
	}
	return map[string]interface{}{
		FieldSchemaVersion: hb.SchemaVersion,
		FieldPayload:       string(payload),
		FieldWorkerID:      hb.WorkerID,
		"type":             streamTypeWorkerHeartbeat,
		"status":           hb.Status,
		"reported_at":      hb.ReportedAt,
	}, nil
}
