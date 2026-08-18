package redis

import (
	"encoding/json"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/contracts"
)

// -- Stream key / group name 常量测试 --

func TestStreamKeyConstantsNotEmpty(t *testing.T) {
	keys := map[string]string{
		"StreamRunQueue":                StreamRunQueue,
		"StreamRunDeadLetter":           StreamRunDeadLetter,
		"StreamWorkerCommandDeadLetter": StreamWorkerCommandDeadLetter,
		"StreamRuntimeEventDeadLetter":  StreamRuntimeEventDeadLetter,
		"StreamWorkerCommand":           StreamWorkerCommand,
		"StreamRuntimeEvent":            StreamRuntimeEvent,
		"StreamWorkerHeartbeat":         StreamWorkerHeartbeat,
		"StreamPendingPermission":       StreamPendingPermission,
	}
	for name, val := range keys {
		if val == "" {
			t.Errorf("%s 为空", name)
		}
		if !isValidRedisKey(val) {
			t.Errorf("%s = %q 不符合 Redis key 命名约定", name, val)
		}
	}
}

func TestStreamKeyPrefix(t *testing.T) {
	streams := []string{
		StreamRunQueue,
		StreamRunDeadLetter,
		StreamWorkerCommandDeadLetter,
		StreamRuntimeEventDeadLetter,
		StreamWorkerCommand,
		StreamRuntimeEvent,
		StreamWorkerHeartbeat,
		StreamPendingPermission,
	}
	for _, s := range streams {
		if s[:7] != "jarvis:" {
			t.Errorf("stream key %q 缺少 jarvis: 前缀", s)
		}
	}
}

func TestGroupNameConstants(t *testing.T) {
	groups := map[string]string{
		"GroupWorkerPool":    GroupWorkerPool,
		"GroupGatewayEvents": GroupGatewayEvents,
	}
	for name, val := range groups {
		if val == "" {
			t.Errorf("%s 为空", name)
		}
		if val[:7] != "jarvis:" {
			t.Errorf("%s = %q 缺少 jarvis: 前缀", name, val)
		}
	}
}

func TestFieldConstantsNotEmpty(t *testing.T) {
	fields := map[string]string{
		"FieldJobID":         FieldJobID,
		"FieldTaskID":        FieldTaskID,
		"FieldRunID":         FieldRunID,
		"FieldEventID":       FieldEventID,
		"FieldCommandID":     FieldCommandID,
		"FieldRequestID":     FieldRequestID,
		"FieldWorkerID":      FieldWorkerID,
		"FieldTraceID":       FieldTraceID,
		"FieldSchemaVersion": FieldSchemaVersion,
		"FieldPayload":       FieldPayload,
	}
	for name, val := range fields {
		if val == "" {
			t.Errorf("%s 为空", name)
		}
	}
}

func TestSchemaVersionStable(t *testing.T) {
	if SchemaVersion == "" {
		t.Error("SchemaVersion 为空")
	}
	if SchemaVersion != "2B-1a.1" {
		t.Errorf("SchemaVersion = %q，期望 \"2B-1a.1\"", SchemaVersion)
	}
}

// -- RunJobMessage 序列化 + Decode 测试 --

func TestRunJobMessageRoundTrip(t *testing.T) {
	original := RunJobMessage{
		JobID:         "job-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		UserGoal:      "读取 README.md 并总结内容",
		WorkspacePath: "/Users/test/project",
		CreatedAt:     "2026-07-03T10:00:00Z",
		SchemaVersion: SchemaVersion,
	}

	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("Marshal RunJobMessage 失败: %v", err)
	}

	var decoded RunJobMessage
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("Unmarshal RunJobMessage 失败: %v", err)
	}

	if decoded.JobID != original.JobID {
		t.Errorf("JobID: got %q, want %q", decoded.JobID, original.JobID)
	}
	if decoded.TraceID != original.TraceID {
		t.Errorf("TraceID: got %q, want %q", decoded.TraceID, original.TraceID)
	}
	if decoded.TaskID != original.TaskID {
		t.Errorf("TaskID: got %q, want %q", decoded.TaskID, original.TaskID)
	}
	if decoded.RunID != original.RunID {
		t.Errorf("RunID: got %q, want %q", decoded.RunID, original.RunID)
	}
	if decoded.UserGoal != original.UserGoal {
		t.Errorf("UserGoal: got %q, want %q", decoded.UserGoal, original.UserGoal)
	}
	if decoded.SchemaVersion != SchemaVersion {
		t.Errorf("SchemaVersion: got %q, want %q", decoded.SchemaVersion, SchemaVersion)
	}
}

func TestDecodeRunJobMessageSuccess(t *testing.T) {
	original := RunJobMessage{
		JobID:         "job-002",
		TraceID:       "trace-002",
		TaskID:        "task-002",
		RunID:         "run-002",
		UserGoal:      "创建新文件",
		WorkspacePath: "",
		CreatedAt:     "2026-07-03T10:00:00Z",
		SchemaVersion: SchemaVersion,
	}

	m, err := ToMap(original)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}

	decoded, err := DecodeRunJobMessage(m)
	if err != nil {
		t.Fatalf("DecodeRunJobMessage 失败: %v", err)
	}

	if decoded.JobID != original.JobID {
		t.Errorf("JobID: got %q, want %q", decoded.JobID, original.JobID)
	}
	if decoded.TraceID != original.TraceID {
		t.Errorf("TraceID: got %q, want %q", decoded.TraceID, original.TraceID)
	}
	if decoded.UserGoal != original.UserGoal {
		t.Errorf("UserGoal: got %q, want %q", decoded.UserGoal, original.UserGoal)
	}
}

func TestDecodeRunJobMessageBadSchemaVersion(t *testing.T) {
	m := map[string]interface{}{
		"job_id":         "job-001",
		"trace_id":       "trace-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"user_goal":      "test",
		"created_at":     "2026-07-03T10:00:00Z",
		"schema_version": "0.0.0-bad",
	}
	_, err := DecodeRunJobMessage(m)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
}

func TestDecodeRunJobMessageMissingJobID(t *testing.T) {
	m := map[string]interface{}{
		"trace_id":       "trace-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"user_goal":      "test",
		"created_at":     "2026-07-03T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodeRunJobMessage(m)
	if err == nil {
		t.Error("缺少 job_id 应返回 error")
	}
}

func TestDecodeRunJobMessageMissingTraceID(t *testing.T) {
	m := map[string]interface{}{
		"job_id":         "job-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"user_goal":      "test",
		"created_at":     "2026-07-03T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodeRunJobMessage(m)
	if err == nil {
		t.Error("缺少 trace_id 应返回 error")
	}
}

func TestDecodeRunJobMessageEmptyJobID(t *testing.T) {
	m := map[string]interface{}{
		"job_id":         "",
		"trace_id":       "trace-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"user_goal":      "test",
		"created_at":     "2026-07-03T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodeRunJobMessage(m)
	if err == nil {
		t.Error("job_id 为空应返回 error")
	}
}

// -- RuntimeEventEnvelope 序列化 + Decode 测试 --

func TestRuntimeEventEnvelopeRoundTrip(t *testing.T) {
	rtEvent := contracts.RuntimeEvent{
		ID:        "evt-001",
		Type:      "tool.call.started",
		TaskID:    "task-001",
		RunID:     "run-001",
		StepID:    "step-001",
		Timestamp: "2026-07-03T10:00:01Z",
		Payload: map[string]interface{}{
			"tool_call": map[string]interface{}{
				"id":        "tc-001",
				"tool_name": "read_file",
				"provider":  "native",
			},
		},
	}

	original := RuntimeEventEnvelope{
		EventID:       "evt-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "tool.call.started",
		RuntimeEvent:  rtEvent,
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
	}

	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("Marshal RuntimeEventEnvelope 失败: %v", err)
	}

	var decoded RuntimeEventEnvelope
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("Unmarshal RuntimeEventEnvelope 失败: %v", err)
	}

	if decoded.EventID != original.EventID {
		t.Errorf("EventID: got %q, want %q", decoded.EventID, original.EventID)
	}
	if decoded.TraceID != original.TraceID {
		t.Errorf("TraceID: got %q, want %q", decoded.TraceID, original.TraceID)
	}
	if decoded.ProducedBy != original.ProducedBy {
		t.Errorf("ProducedBy: got %q, want %q", decoded.ProducedBy, original.ProducedBy)
	}
	if decoded.SchemaVersion != SchemaVersion {
		t.Errorf("SchemaVersion: got %q, want %q", decoded.SchemaVersion, SchemaVersion)
	}

	inner := decoded.RuntimeEvent
	if inner.ID != rtEvent.ID {
		t.Errorf("RuntimeEvent.ID: got %q, want %q", inner.ID, rtEvent.ID)
	}
	if inner.Type != rtEvent.Type {
		t.Errorf("RuntimeEvent.Type: got %q, want %q", inner.Type, rtEvent.Type)
	}

	tc, ok := inner.Payload["tool_call"].(map[string]interface{})
	if !ok {
		t.Fatal("RuntimeEvent.Payload.tool_call 不是 map")
	}
	if tc["tool_name"] != "read_file" {
		t.Errorf("tool_name: got %q, want %q", tc["tool_name"], "read_file")
	}
}

func TestDecodeRuntimeEventEnvelopeSuccess(t *testing.T) {
	rtEvent := contracts.RuntimeEvent{
		ID:        "evt-002",
		Type:      "permission.required",
		TaskID:    "task-001",
		RunID:     "run-001",
		StepID:    "step-001",
		Timestamp: "2026-07-03T10:00:00Z",
		Payload: map[string]interface{}{
			"request": map[string]interface{}{
				"id":         "pr-001",
				"tool_name":  "shell",
				"risk_level": "L3",
			},
		},
	}

	env := RuntimeEventEnvelope{
		EventID:       "evt-002",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "permission.required",
		RuntimeEvent:  rtEvent,
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
	}

	m, err := ToMap(env)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}

	decoded, err := DecodeRuntimeEventEnvelope(m)
	if err != nil {
		t.Fatalf("DecodeRuntimeEventEnvelope 失败: %v", err)
	}

	if decoded.EventID != env.EventID {
		t.Errorf("EventID: got %q, want %q", decoded.EventID, env.EventID)
	}
	if decoded.TraceID != env.TraceID {
		t.Errorf("TraceID: got %q, want %q", decoded.TraceID, env.TraceID)
	}
	// 内层核心字段
	if decoded.RuntimeEvent.ID != "evt-002" {
		t.Errorf("RuntimeEvent.ID: got %q", decoded.RuntimeEvent.ID)
	}
}

func TestDecodeRuntimeEventEnvelopeMissingTraceID(t *testing.T) {
	rtEvent := contracts.RuntimeEvent{
		ID:        "evt-001",
		Type:      "task.created",
		TaskID:    "task-001",
		RunID:     "run-001",
		Timestamp: "2026-07-03T10:00:00Z",
		Payload:   map[string]interface{}{},
	}

	env := RuntimeEventEnvelope{
		EventID:       "evt-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "task.created",
		RuntimeEvent:  rtEvent,
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
	}

	m, err := ToMap(env)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}
	// 删除 trace_id
	delete(m, "trace_id")

	_, err = DecodeRuntimeEventEnvelope(m)
	if err == nil {
		t.Error("缺少 trace_id 应返回 error")
	}
}

func TestDecodeRuntimeEventEnvelopeEventTypeMismatch(t *testing.T) {
	rtEvent := contracts.RuntimeEvent{
		ID:        "evt-001",
		Type:      "tool.call.started",
		TaskID:    "task-001",
		RunID:     "run-001",
		Timestamp: "2026-07-03T10:00:00Z",
		Payload:   map[string]interface{}{},
	}

	env := RuntimeEventEnvelope{
		EventID:       "evt-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "agent.run.completed", // 与 runtime_event.type 不一致
		RuntimeEvent:  rtEvent,
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
	}

	m, err := ToMap(env)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}

	_, err = DecodeRuntimeEventEnvelope(m)
	if err == nil {
		t.Error("event_type 不一致应返回 error")
	}
}

func TestDecodeRuntimeEventEnvelopeEventIDMismatch(t *testing.T) {
	rtEvent := contracts.RuntimeEvent{
		ID: "evt-inner", Type: "task.created", TaskID: "task-001",
		RunID: "run-001", Timestamp: "2026-07-03T10:00:00Z",
		Payload: map[string]interface{}{},
	}
	env := RuntimeEventEnvelope{
		EventID: "evt-outer", TraceID: "trace-001", TaskID: "task-001",
		RunID: "run-001", EventType: "task.created", RuntimeEvent: rtEvent,
		ProducedBy: "worker-01", SchemaVersion: SchemaVersion,
	}
	m, err := ToMap(env)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = DecodeRuntimeEventEnvelope(m); err == nil {
		t.Error("event_id 与 runtime_event.id 不一致应返回 error")
	}
}

func TestDecodeRuntimeEventEnvelopeTaskIDMismatch(t *testing.T) {
	rtEvent := contracts.RuntimeEvent{
		ID:        "evt-001",
		Type:      "task.created",
		TaskID:    "task-inner", // 与 envelope 层不一致
		RunID:     "run-001",
		Timestamp: "2026-07-03T10:00:00Z",
		Payload:   map[string]interface{}{},
	}

	env := RuntimeEventEnvelope{
		EventID:       "evt-001",
		TraceID:       "trace-001",
		TaskID:        "task-outer",
		RunID:         "run-001",
		EventType:     "task.created",
		RuntimeEvent:  rtEvent,
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
	}

	m, err := ToMap(env)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}

	_, err = DecodeRuntimeEventEnvelope(m)
	if err == nil {
		t.Error("task_id 不一致应返回 error")
	}
}

func TestDecodeRuntimeEventEnvelopeInnerMissingTimestamp(t *testing.T) {
	rtEvent := contracts.RuntimeEvent{
		ID:      "evt-001",
		Type:    "task.created",
		TaskID:  "task-001",
		RunID:   "run-001",
		Payload: map[string]interface{}{},
		// Timestamp 为空
	}

	env := RuntimeEventEnvelope{
		EventID:       "evt-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "task.created",
		RuntimeEvent:  rtEvent,
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
	}

	m, err := ToMap(env)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}

	_, err = DecodeRuntimeEventEnvelope(m)
	if err == nil {
		t.Error("runtime_event.timestamp 为空应返回 error")
	}
}

func TestDecodeRuntimeEventEnvelopeBadSchemaVersion(t *testing.T) {
	rtEvent := contracts.RuntimeEvent{
		ID:        "evt-001",
		Type:      "task.created",
		TaskID:    "task-001",
		RunID:     "run-001",
		Timestamp: "2026-07-03T10:00:00Z",
		Payload:   map[string]interface{}{},
	}

	env := RuntimeEventEnvelope{
		EventID:       "evt-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "task.created",
		RuntimeEvent:  rtEvent,
		ProducedBy:    "worker-01",
		SchemaVersion: "9.9.9-wrong",
	}

	m, err := ToMap(env)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}

	_, err = DecodeRuntimeEventEnvelope(m)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
}

// -- PermissionDecisionCommand 序列化 + Decode 测试 --

func TestPermissionDecisionCommandRoundTrip(t *testing.T) {
	original := PermissionDecisionCommand{
		CommandID:     "cmd-001",
		TraceID:       "trace-001",
		RequestID:     "pr-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		Decision:      "allow_once",
		Note:          "这次允许读取该文件",
		DecidedAt:     "2026-07-03T10:00:05Z",
		SchemaVersion: SchemaVersion,
	}

	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("Marshal PermissionDecisionCommand 失败: %v", err)
	}

	var decoded PermissionDecisionCommand
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("Unmarshal PermissionDecisionCommand 失败: %v", err)
	}

	if decoded.RequestID != original.RequestID {
		t.Errorf("RequestID: got %q, want %q", decoded.RequestID, original.RequestID)
	}
	if decoded.TraceID != original.TraceID {
		t.Errorf("TraceID: got %q, want %q", decoded.TraceID, original.TraceID)
	}
	if decoded.Decision != original.Decision {
		t.Errorf("Decision: got %q, want %q", decoded.Decision, original.Decision)
	}
	if decoded.Note != original.Note {
		t.Errorf("Note: got %q, want %q", decoded.Note, original.Note)
	}
}

func TestDecodePermissionDecisionCommandDenyWithNote(t *testing.T) {
	original := PermissionDecisionCommand{
		CommandID:     "cmd-002",
		TraceID:       "trace-002",
		RequestID:     "pr-002",
		TaskID:        "task-002",
		RunID:         "run-002",
		Decision:      "deny",
		Note:          "不允许删除系统文件",
		DecidedAt:     "2026-07-03T10:00:10Z",
		SchemaVersion: SchemaVersion,
	}

	m, err := ToMap(original)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}

	decoded, err := DecodePermissionDecisionCommand(m)
	if err != nil {
		t.Fatalf("DecodePermissionDecisionCommand 失败: %v", err)
	}

	if decoded.RequestID != "pr-002" {
		t.Errorf("RequestID: got %q", decoded.RequestID)
	}
	if decoded.Decision != "deny" {
		t.Errorf("Decision: got %q", decoded.Decision)
	}
	if decoded.Note != "不允许删除系统文件" {
		t.Errorf("Note: got %q", decoded.Note)
	}
	if decoded.TraceID != "trace-002" {
		t.Errorf("TraceID: got %q", decoded.TraceID)
	}
}

func TestDecodePermissionDecisionCommandMissingRequestID(t *testing.T) {
	m := map[string]interface{}{
		"command_id":     "cmd-001",
		"trace_id":       "trace-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"decision":       "allow_once",
		"decided_at":     "2026-07-03T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodePermissionDecisionCommand(m)
	if err == nil {
		t.Error("缺少 request_id 应返回 error")
	}
}

func TestDecodePermissionDecisionCommandMissingTraceID(t *testing.T) {
	m := map[string]interface{}{
		"command_id":     "cmd-001",
		"request_id":     "pr-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"decision":       "allow_once",
		"decided_at":     "2026-07-03T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodePermissionDecisionCommand(m)
	if err == nil {
		t.Error("缺少 trace_id 应返回 error")
	}
}

func TestDecodePermissionDecisionCommandEmptyDecision(t *testing.T) {
	m := map[string]interface{}{
		"command_id":     "cmd-001",
		"trace_id":       "trace-001",
		"request_id":     "pr-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"decision":       "",
		"decided_at":     "2026-07-03T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodePermissionDecisionCommand(m)
	if err == nil {
		t.Error("decision 为空应返回 error")
	}
}

func TestDecodePermissionDecisionCommandBadSchemaVersion(t *testing.T) {
	m := map[string]interface{}{
		"command_id":     "cmd-001",
		"trace_id":       "trace-001",
		"request_id":     "pr-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"decision":       "allow_once",
		"decided_at":     "2026-07-03T10:00:00Z",
		"schema_version": "1.0.0-old",
	}
	_, err := DecodePermissionDecisionCommand(m)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
}

// -- RunCancelCommand 序列化 + Decode 测试（3C cancel）--

func TestRunCancelCommandRoundTrip(t *testing.T) {
	original := RunCancelCommand{
		CommandID:     "cmd-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		Type:          "run.cancel",
		RequestedAt:   "2026-07-07T10:00:00Z",
		SchemaVersion: SchemaVersion,
	}

	m, err := ToMap(original)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}

	decoded, err := DecodeRunCancelCommand(m)
	if err != nil {
		t.Fatalf("DecodeRunCancelCommand 失败: %v", err)
	}

	if decoded.CommandID != original.CommandID {
		t.Errorf("CommandID: got %q, want %q", decoded.CommandID, original.CommandID)
	}
	if decoded.Type != "run.cancel" {
		t.Errorf("Type: got %q, want run.cancel", decoded.Type)
	}
	if decoded.RunID != original.RunID {
		t.Errorf("RunID: got %q, want %q", decoded.RunID, original.RunID)
	}
}

func TestDecodeRunCancelCommandUnsupportedType(t *testing.T) {
	m := map[string]interface{}{
		"command_id":     "cmd-001",
		"trace_id":       "trace-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"type":           "run.pause", // 不支持
		"requested_at":   "2026-07-07T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodeRunCancelCommand(m)
	if err == nil {
		t.Error("不支持的 command type 应返回 error")
	}
}

func TestDecodeRunCancelCommandMissingRunID(t *testing.T) {
	m := map[string]interface{}{
		"command_id":     "cmd-001",
		"trace_id":       "trace-001",
		"task_id":        "task-001",
		"type":           "run.cancel",
		"requested_at":   "2026-07-07T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodeRunCancelCommand(m)
	if err == nil {
		t.Error("缺少 run_id 应返回 error")
	}
}

func TestDecodeRunCancelCommandBadSchemaVersion(t *testing.T) {
	m := map[string]interface{}{
		"command_id":     "cmd-001",
		"trace_id":       "trace-001",
		"task_id":        "task-001",
		"run_id":         "run-001",
		"type":           "run.cancel",
		"requested_at":   "2026-07-07T10:00:00Z",
		"schema_version": "bad-ver",
	}
	_, err := DecodeRunCancelCommand(m)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
}

// -- WorkerHeartbeatMessage 序列化 + Decode 测试 --

func TestWorkerHeartbeatMessageRoundTrip(t *testing.T) {
	original := WorkerHeartbeatMessage{
		WorkerID:      "worker-01",
		Status:        "busy",
		ActiveRunID:   "run-001",
		ReportedAt:    "2026-07-03T10:00:00Z",
		SchemaVersion: SchemaVersion,
	}

	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("Marshal WorkerHeartbeatMessage 失败: %v", err)
	}

	var decoded WorkerHeartbeatMessage
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("Unmarshal WorkerHeartbeatMessage 失败: %v", err)
	}

	if decoded.WorkerID != original.WorkerID {
		t.Errorf("WorkerID: got %q, want %q", decoded.WorkerID, original.WorkerID)
	}
	if decoded.Status != original.Status {
		t.Errorf("Status: got %q, want %q", decoded.Status, original.Status)
	}
	if decoded.ActiveRunID != original.ActiveRunID {
		t.Errorf("ActiveRunID: got %q, want %q", decoded.ActiveRunID, original.ActiveRunID)
	}
	if decoded.SchemaVersion != SchemaVersion {
		t.Errorf("SchemaVersion: got %q, want %q", decoded.SchemaVersion, SchemaVersion)
	}
}

func TestDecodeWorkerHeartbeatMessageIdle(t *testing.T) {
	original := WorkerHeartbeatMessage{
		WorkerID:      "worker-02",
		Status:        "idle",
		ActiveRunID:   "",
		ReportedAt:    "2026-07-03T10:00:00Z",
		SchemaVersion: SchemaVersion,
	}

	m, err := ToMap(original)
	if err != nil {
		t.Fatalf("ToMap 失败: %v", err)
	}

	decoded, err := DecodeWorkerHeartbeatMessage(m)
	if err != nil {
		t.Fatalf("DecodeWorkerHeartbeatMessage 失败: %v", err)
	}

	if decoded.Status != "idle" {
		t.Errorf("Status: got %q, want %q", decoded.Status, "idle")
	}
	if decoded.ActiveRunID != "" {
		t.Errorf("idle worker 的 ActiveRunID 应为空，got %q", decoded.ActiveRunID)
	}
}

func TestDecodeWorkerHeartbeatMessageMissingWorkerID(t *testing.T) {
	m := map[string]interface{}{
		"status":         "idle",
		"reported_at":    "2026-07-03T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodeWorkerHeartbeatMessage(m)
	if err == nil {
		t.Error("缺少 worker_id 应返回 error")
	}
}

func TestDecodeWorkerHeartbeatMessageBadSchemaVersion(t *testing.T) {
	m := map[string]interface{}{
		"worker_id":      "worker-01",
		"status":         "idle",
		"reported_at":    "2026-07-03T10:00:00Z",
		"schema_version": "0.0.0-wrong",
	}
	_, err := DecodeWorkerHeartbeatMessage(m)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
}

func TestDecodeWorkerHeartbeatMessageInvalidStatus(t *testing.T) {
	// "sleeping" 不是合法 status
	m := map[string]interface{}{
		"worker_id":      "worker-01",
		"status":         "sleeping",
		"reported_at":    "2026-07-07T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodeWorkerHeartbeatMessage(m)
	if err == nil {
		t.Error("非法 status 'sleeping' 应返回 error")
	}
}

func TestDecodeWorkerHeartbeatMessageBusyEmptyActiveRunID(t *testing.T) {
	// 缺省类型是 agent；busy 状态必须 active_run_id 非空
	m := map[string]interface{}{
		"worker_id":      "worker-01",
		"status":         "busy",
		"reported_at":    "2026-07-07T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodeWorkerHeartbeatMessage(m)
	if err == nil {
		t.Error("busy + 空 active_run_id 应返回 error")
	}
}

func TestDecodeRagWorkerHeartbeatBusyWithoutActiveRunID(t *testing.T) {
	m := map[string]interface{}{
		"worker_id":      "rag-worker-01",
		"worker_kind":    "rag",
		"status":         "busy",
		"reported_at":    "2026-07-07T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	decoded, err := DecodeWorkerHeartbeatMessage(m)
	if err != nil {
		t.Fatalf("RAG busy 不应要求 active_run_id: %v", err)
	}
	if decoded.WorkerKind != "rag" || decoded.ActiveRunID != "" {
		t.Fatalf("RAG heartbeat 解码错误: %#v", decoded)
	}
}

func TestDecodeWorkerHeartbeatRejectsUnknownKind(t *testing.T) {
	m := map[string]interface{}{
		"worker_id":      "worker-01",
		"worker_kind":    "unknown",
		"status":         "idle",
		"reported_at":    "2026-07-07T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	if _, err := DecodeWorkerHeartbeatMessage(m); err == nil {
		t.Fatal("非法 worker_kind 应返回 error")
	}
}

func TestDecodeWorkerHeartbeatMessageBusyWithActiveRunID(t *testing.T) {
	// busy + active_run_id → 通过
	m := map[string]interface{}{
		"worker_id":      "worker-01",
		"status":         "busy",
		"active_run_id":  "run-001",
		"reported_at":    "2026-07-07T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	decoded, err := DecodeWorkerHeartbeatMessage(m)
	if err != nil {
		t.Fatalf("busy + valid active_run_id 应通过: %v", err)
	}
	if decoded.Status != "busy" {
		t.Errorf("status = %q", decoded.Status)
	}
	if decoded.ActiveRunID != "run-001" {
		t.Errorf("active_run_id = %q", decoded.ActiveRunID)
	}
}

func TestDecodeWorkerHeartbeatMessageIdleEmptyActiveRunID(t *testing.T) {
	// idle + 空 active_run_id → 通过
	m := map[string]interface{}{
		"worker_id":      "worker-01",
		"status":         "idle",
		"reported_at":    "2026-07-07T10:00:00Z",
		"schema_version": SchemaVersion,
	}
	_, err := DecodeWorkerHeartbeatMessage(m)
	if err != nil {
		t.Errorf("idle + 空 active_run_id 应通过: %v", err)
	}
}

func TestDecodeWorkerHeartbeatMessageAllValidStatuses(t *testing.T) {
	// 所有合法 status 均通过
	validStatuses := []string{"starting", "idle", "busy", "draining", "stopped", "failed"}
	for _, s := range validStatuses {
		m := map[string]interface{}{
			"worker_id":      "worker-01",
			"status":         s,
			"reported_at":    "2026-07-07T10:00:00Z",
			"schema_version": SchemaVersion,
		}
		if s == "busy" {
			m["active_run_id"] = "run-001"
		}
		_, err := DecodeWorkerHeartbeatMessage(m)
		if err != nil {
			t.Errorf("合法 status %q 应通过: %v", s, err)
		}
	}
}

// -- ValidateSchemaVersion 精确匹配测试 --

func TestValidateSchemaVersionExactMatch(t *testing.T) {
	m := map[string]interface{}{
		"schema_version": SchemaVersion,
	}
	if err := ValidateSchemaVersion(m); err != nil {
		t.Errorf("精确匹配 %q 应返回 nil，got: %v", SchemaVersion, err)
	}
}

func TestValidateSchemaVersionBadVersion(t *testing.T) {
	versions := []string{"0.0.0", "2B-1a.2", "1.0.0", "unknown"}
	for _, v := range versions {
		m := map[string]interface{}{"schema_version": v}
		err := ValidateSchemaVersion(m)
		if err == nil {
			t.Errorf("version %q 应返回 error", v)
		}
	}
}

func TestValidateSchemaVersionMissing(t *testing.T) {
	m := map[string]interface{}{
		"job_id": "job-001",
	}
	err := ValidateSchemaVersion(m)
	if err == nil {
		t.Error("缺少 schema_version 应返回 error")
	}
}

func TestValidateSchemaVersionEmpty(t *testing.T) {
	m := map[string]interface{}{
		"schema_version": "",
	}
	err := ValidateSchemaVersion(m)
	if err == nil {
		t.Error("schema_version 为空应返回 error")
	}
}

func TestValidateSchemaVersionNonString(t *testing.T) {
	m := map[string]interface{}{
		"schema_version": 123,
	}
	err := ValidateSchemaVersion(m)
	if err == nil {
		t.Error("schema_version 不是字符串应返回 error")
	}
}

// -- 辅助函数 --

// isValidRedisKey 做宽松校验：非空、无空格、只含 ASCII 可打印字符和冒号/连字符。
func isValidRedisKey(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r == ' ' {
			return false
		}
		if r < 0x20 || r > 0x7e {
			return false
		}
	}
	return true
}

// -- Phase 6B-1: model status decode --

func TestDecodeWorkerHeartbeatMessageWithModel(t *testing.T) {
	m := map[string]interface{}{
		"worker_id":      "worker-model",
		"status":         "idle",
		"reported_at":    "2026-07-10T10:00:00Z",
		"schema_version": SchemaVersion,
		"model": map[string]interface{}{
			"provider":           "deepseek",
			"protocol":           "openai_chat_completions",
			"model_name":         "deepseek-v4-flash",
			"api_key_configured": true,
			"thinking_mode":      "disabled",
			"status":             "configured",
			"last_error_code":    nil,
		},
	}

	decoded, err := DecodeWorkerHeartbeatMessage(m)
	if err != nil {
		t.Fatalf("DecodeWorkerHeartbeatMessage 失败: %v", err)
	}

	if decoded.Model == nil {
		t.Fatal("decoded.Model 应为非 nil")
	}
	if decoded.Model.Provider != "deepseek" {
		t.Errorf("Model.Provider: got %q, want %q", decoded.Model.Provider, "deepseek")
	}
	if decoded.Model.ModelName != "deepseek-v4-flash" {
		t.Errorf("Model.ModelName: got %q, want %q", decoded.Model.ModelName, "deepseek-v4-flash")
	}
	if !decoded.Model.APIKeyConfigured {
		t.Error("Model.APIKeyConfigured 应为 true")
	}
	if decoded.Model.ThinkingMode != "disabled" {
		t.Errorf("Model.ThinkingMode: got %q, want %q", decoded.Model.ThinkingMode, "disabled")
	}
	if decoded.Model.Status != "configured" {
		t.Errorf("Model.Status: got %q, want %q", decoded.Model.Status, "configured")
	}
}

func TestDecodeWorkerHeartbeatMessageWithoutModel(t *testing.T) {
	// 旧 heartbeat 无 model 字段 → 兼容，不报错
	m := map[string]interface{}{
		"worker_id":      "worker-old",
		"status":         "idle",
		"reported_at":    "2026-07-10T10:00:00Z",
		"schema_version": SchemaVersion,
	}

	decoded, err := DecodeWorkerHeartbeatMessage(m)
	if err != nil {
		t.Fatalf("DecodeWorkerHeartbeatMessage 失败: %v", err)
	}
	if decoded.Model != nil {
		t.Errorf("旧 heartbeat 无 model 时 Model 应为 nil，got %+v", decoded.Model)
	}
}

func TestWorkerHeartbeatModelStatusMarshalIncludesNullLastErrorCode(t *testing.T) {
	original := WorkerHeartbeatMessage{
		WorkerID:      "worker-model",
		Status:        "idle",
		ReportedAt:    "2026-07-10T10:00:00Z",
		SchemaVersion: SchemaVersion,
		Model: &WorkerModelStatus{
			Provider:         "deepseek",
			Protocol:         "openai_chat_completions",
			ModelName:        "deepseek-v4-flash",
			APIKeyConfigured: true,
			ThinkingMode:     "disabled",
			Status:           "configured",
			LastErrorCode:    nil,
		},
	}

	data, err := json.Marshal(original)
	if err != nil {
		t.Fatalf("Marshal 失败: %v", err)
	}

	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("Unmarshal 失败: %v", err)
	}

	model, ok := raw["model"].(map[string]interface{})
	if !ok {
		t.Fatal("payload 中应包含 model 字段")
	}

	if _, exists := model["last_error_code"]; !exists {
		t.Error("last_error_code key 必须存在（即使值为 null）")
	}
	if model["last_error_code"] != nil {
		t.Errorf("last_error_code 应为 nil，got %v", model["last_error_code"])
	}
}
