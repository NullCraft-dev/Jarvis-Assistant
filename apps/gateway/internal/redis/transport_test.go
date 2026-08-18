package redis

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/contracts"
)

// -- fake client for testing --

// streamCall 记录一次 XAdd 调用信息。
type streamCall struct {
	Stream string
	Values map[string]interface{}
}

// fakeStreamClient 是 RedisStreamClient 的测试替身。
// 记录每次 XAdd 调用的 stream key 和 values，并可注入返回错误。
type fakeStreamClient struct {
	Calls   []streamCall
	XAddErr error
}

// 确保 fakeStreamClient 实现 RedisStreamClient 接口。
var _ RedisStreamClient = (*fakeStreamClient)(nil)

// XAdd 记录调用信息并返回注入的错误（如有）。
func (f *fakeStreamClient) XAdd(_ context.Context, stream string, values map[string]interface{}) error {
	f.Calls = append(f.Calls, streamCall{Stream: stream, Values: values})
	return f.XAddErr
}

// reset 清空调用记录（测试 helper）。
func (f *fakeStreamClient) reset() {
	f.Calls = nil
	f.XAddErr = nil
}

// -- helpers --

func newFakeClient() *fakeStreamClient {
	return &fakeStreamClient{}
}

// newTestTransport 创建注入 fake client 的 transport，测试用。
func newTestTransport(fc *fakeStreamClient) *RedisRuntimeTransport {
	tr, err := NewRedisRuntimeTransport(fc)
	if err != nil {
		panic(fmt.Sprintf("newTestTransport: %v", err))
	}
	return tr
}

func validRunJob() RunJobMessage {
	return RunJobMessage{
		JobID:         "job-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		UserGoal:      "读取文件",
		WorkspacePath: "/tmp/test",
		CreatedAt:     "2026-07-03T10:00:00Z",
		SchemaVersion: SchemaVersion,
	}
}

func validPermissionDecision() PermissionDecisionCommand {
	return PermissionDecisionCommand{
		CommandID:     "cmd-001",
		TraceID:       "trace-001",
		RequestID:     "pr-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		Decision:      "allow_once",
		Note:          "允许",
		DecidedAt:     "2026-07-03T10:00:00Z",
		SchemaVersion: SchemaVersion,
	}
}

func validRuntimeEventEnvelope() RuntimeEventEnvelope {
	return RuntimeEventEnvelope{
		EventID:   "evt-001",
		TraceID:   "trace-001",
		TaskID:    "task-001",
		RunID:     "run-001",
		EventType: "tool.call.started",
		RuntimeEvent: contracts.RuntimeEvent{
			ID:        "evt-001",
			Type:      "tool.call.started",
			TaskID:    "task-001",
			RunID:     "run-001",
			StepID:    "step-001",
			Timestamp: "2026-07-03T10:00:00Z",
			Payload: map[string]interface{}{
				"tool_call": map[string]interface{}{
					"id":        "tc-001",
					"tool_name": "read_file",
				},
			},
		},
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
	}
}

func validHeartbeat() WorkerHeartbeatMessage {
	return WorkerHeartbeatMessage{
		WorkerID:      "worker-01",
		Status:        "busy",
		ActiveRunID:   "run-001",
		ReportedAt:    "2026-07-03T10:00:00Z",
		SchemaVersion: SchemaVersion,
		RuntimeBus: &WorkerRuntimeBusMetrics{
			Reclaimed: 2, RetryDeferred: 1, DeadLettered: 1, Malformed: 1,
			CommandReclaimed: 3, CommandDeadLettered: 1, CommandMalformed: 1,
		},
	}
}

// -- nil guard 测试 --

func TestNewRedisRuntimeTransportNilClient(t *testing.T) {
	tr, err := NewRedisRuntimeTransport(nil)
	if err == nil {
		t.Error("nil client 应返回 error")
	}
	if tr != nil {
		t.Error("nil client 时 transport 应为 nil")
	}
}

func TestNewGoRedisStreamClientNilClient(t *testing.T) {
	gc, err := NewGoRedisStreamClient(nil)
	if err == nil {
		t.Error("nil *redis.Client 应返回 error")
	}
	if gc != nil {
		t.Error("nil client 时 GoRedisStreamClient 应为 nil")
	}
}

// -- EnqueueRunJob 测试 --

func TestEnqueueRunJobSuccess(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	msg := validRunJob()

	err := tr.EnqueueRunJob(context.Background(), msg)
	if err != nil {
		t.Fatalf("EnqueueRunJob 失败: %v", err)
	}

	if len(fc.Calls) != 1 {
		t.Fatalf("期望 1 次 XAdd 调用，got %d", len(fc.Calls))
	}

	call := fc.Calls[0]
	if call.Stream != StreamRunQueue {
		t.Errorf("stream: got %q, want %q", call.Stream, StreamRunQueue)
	}

	vals := call.Values

	// 标量路由字段
	if vals[FieldSchemaVersion] != SchemaVersion {
		t.Errorf("schema_version: got %v, want %q", vals[FieldSchemaVersion], SchemaVersion)
	}
	if vals[FieldTraceID] != "trace-001" {
		t.Errorf("trace_id: got %v, want %q", vals[FieldTraceID], "trace-001")
	}
	if vals[FieldJobID] != "job-001" {
		t.Errorf("job_id: got %v, want %q", vals[FieldJobID], "job-001")
	}
	if vals[FieldTaskID] != "task-001" {
		t.Errorf("task_id: got %v, want %q", vals[FieldTaskID], "task-001")
	}
	if vals[FieldRunID] != "run-001" {
		t.Errorf("run_id: got %v, want %q", vals[FieldRunID], "run-001")
	}
	if vals["type"] != streamTypeRunJob {
		t.Errorf("type: got %v, want %q", vals["type"], streamTypeRunJob)
	}
	if vals["created_at"] != "2026-07-03T10:00:00Z" {
		t.Errorf("created_at: got %v", vals["created_at"])
	}

	// payload 是完整 JSON 字符串，可 decode 回 RunJobMessage
	payloadStr, ok := vals[FieldPayload].(string)
	if !ok {
		t.Fatalf("payload 不是 string，got %T", vals[FieldPayload])
	}
	var decoded RunJobMessage
	if err := json.Unmarshal([]byte(payloadStr), &decoded); err != nil {
		t.Fatalf("payload JSON decode 失败: %v", err)
	}
	if decoded.JobID != msg.JobID {
		t.Errorf("payload JobID: got %q, want %q", decoded.JobID, msg.JobID)
	}
	if decoded.TraceID != msg.TraceID {
		t.Errorf("payload TraceID: got %q, want %q", decoded.TraceID, msg.TraceID)
	}
	if decoded.UserGoal != msg.UserGoal {
		t.Errorf("payload UserGoal: got %q, want %q", decoded.UserGoal, msg.UserGoal)
	}
	if decoded.SchemaVersion != SchemaVersion {
		t.Errorf("payload SchemaVersion: got %q", decoded.SchemaVersion)
	}
}

func TestEnqueueRunJobMissingTraceID(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	msg := validRunJob()
	msg.TraceID = ""

	err := tr.EnqueueRunJob(context.Background(), msg)
	if err == nil {
		t.Error("缺 trace_id 应返回 error")
	}
	if len(fc.Calls) != 0 {
		t.Errorf("缺 trace_id 不应调用 XAdd，但调用了 %d 次", len(fc.Calls))
	}
}

func TestEnqueueRunJobBadSchemaVersion(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	msg := validRunJob()
	msg.SchemaVersion = "0.0.0-bad"

	err := tr.EnqueueRunJob(context.Background(), msg)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
	if len(fc.Calls) != 0 {
		t.Errorf("bad schema_version 不应调用 XAdd，但调用了 %d 次", len(fc.Calls))
	}
}

func TestEnqueueRunJobXAddError(t *testing.T) {
	fc := newFakeClient()
	fc.XAddErr = errors.New("redis: connection refused")
	tr := newTestTransport(fc)
	msg := validRunJob()

	err := tr.EnqueueRunJob(context.Background(), msg)
	if err == nil {
		t.Error("XAdd 返回 error 时 EnqueueRunJob 应返回 error")
	}
	if len(fc.Calls) != 1 {
		t.Errorf("期望 1 次 XAdd 调用，got %d", len(fc.Calls))
	}
}

// -- PublishPermissionDecision 测试 --

func TestPublishPermissionDecisionSuccess(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	cmd := validPermissionDecision()

	err := tr.PublishPermissionDecision(context.Background(), cmd)
	if err != nil {
		t.Fatalf("PublishPermissionDecision 失败: %v", err)
	}

	if len(fc.Calls) != 1 {
		t.Fatalf("期望 1 次 XAdd 调用，got %d", len(fc.Calls))
	}

	call := fc.Calls[0]
	if call.Stream != StreamWorkerCommand {
		t.Errorf("stream: got %q, want %q", call.Stream, StreamWorkerCommand)
	}

	vals := call.Values
	if vals[FieldSchemaVersion] != SchemaVersion {
		t.Errorf("schema_version: got %v", vals[FieldSchemaVersion])
	}
	if vals[FieldTraceID] != "trace-001" {
		t.Errorf("trace_id: got %v", vals[FieldTraceID])
	}
	if vals[FieldRequestID] != "pr-001" {
		t.Errorf("request_id: got %v", vals[FieldRequestID])
	}
	if vals["type"] != streamTypePermissionDecision {
		t.Errorf("type: got %v, want %q", vals["type"], streamTypePermissionDecision)
	}

	// payload JSON decode
	payloadStr, ok := vals[FieldPayload].(string)
	if !ok {
		t.Fatalf("payload 不是 string，got %T", vals[FieldPayload])
	}
	var decoded PermissionDecisionCommand
	if err := json.Unmarshal([]byte(payloadStr), &decoded); err != nil {
		t.Fatalf("payload JSON decode 失败: %v", err)
	}
	if decoded.RequestID != cmd.RequestID {
		t.Errorf("payload RequestID: got %q, want %q", decoded.RequestID, cmd.RequestID)
	}
	if decoded.Decision != cmd.Decision {
		t.Errorf("payload Decision: got %q, want %q", decoded.Decision, cmd.Decision)
	}
	if decoded.Note != cmd.Note {
		t.Errorf("payload Note: got %q, want %q", decoded.Note, cmd.Note)
	}
	if decoded.TraceID != cmd.TraceID {
		t.Errorf("payload TraceID: got %q, want %q", decoded.TraceID, cmd.TraceID)
	}
}

func TestPublishPermissionDecisionBadSchemaVersion(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	cmd := validPermissionDecision()
	cmd.SchemaVersion = "1.0.0-wrong"

	err := tr.PublishPermissionDecision(context.Background(), cmd)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
	if len(fc.Calls) != 0 {
		t.Errorf("bad schema_version 不应调用 XAdd，但调用了 %d 次", len(fc.Calls))
	}
}

func TestPublishPermissionDecisionXAddError(t *testing.T) {
	fc := newFakeClient()
	fc.XAddErr = errors.New("redis: timeout")
	tr := newTestTransport(fc)
	cmd := validPermissionDecision()

	err := tr.PublishPermissionDecision(context.Background(), cmd)
	if err == nil {
		t.Error("XAdd 返回 error 时应返回 error")
	}
}

// -- PublishRuntimeEvent 测试 --

func TestPublishRuntimeEventSuccess(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	env := validRuntimeEventEnvelope()

	err := tr.PublishRuntimeEvent(context.Background(), env)
	if err != nil {
		t.Fatalf("PublishRuntimeEvent 失败: %v", err)
	}

	if len(fc.Calls) != 1 {
		t.Fatalf("期望 1 次 XAdd 调用，got %d", len(fc.Calls))
	}

	call := fc.Calls[0]
	if call.Stream != StreamRuntimeEvent {
		t.Errorf("stream: got %q, want %q", call.Stream, StreamRuntimeEvent)
	}

	vals := call.Values
	if vals[FieldSchemaVersion] != SchemaVersion {
		t.Errorf("schema_version 不正确")
	}
	if vals[FieldTraceID] != "trace-001" {
		t.Errorf("trace_id 不正确")
	}
	if vals["type"] != "tool.call.started" {
		t.Errorf("type: got %v, want %q", vals["type"], "tool.call.started")
	}

	// payload JSON decode
	payloadStr, ok := vals[FieldPayload].(string)
	if !ok {
		t.Fatalf("payload 不是 string，got %T", vals[FieldPayload])
	}
	var decoded RuntimeEventEnvelope
	if err := json.Unmarshal([]byte(payloadStr), &decoded); err != nil {
		t.Fatalf("payload JSON decode 失败: %v", err)
	}
	if decoded.RuntimeEvent.ID != "evt-001" {
		t.Errorf("payload runtime_event.id: got %q", decoded.RuntimeEvent.ID)
	}
	if decoded.RuntimeEvent.Type != "tool.call.started" {
		t.Errorf("payload runtime_event.type: got %q", decoded.RuntimeEvent.Type)
	}
	tc, ok := decoded.RuntimeEvent.Payload["tool_call"].(map[string]interface{})
	if !ok {
		t.Fatal("payload runtime_event.payload.tool_call 不是 map")
	}
	if tc["tool_name"] != "read_file" {
		t.Errorf("payload tool_call.tool_name: got %v", tc["tool_name"])
	}
}

func TestPublishRuntimeEventEventTypeMismatch(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	env := validRuntimeEventEnvelope()
	env.EventType = "agent.run.completed"

	err := tr.PublishRuntimeEvent(context.Background(), env)
	if err == nil {
		t.Error("event_type 不一致应返回 error")
	}
	if len(fc.Calls) != 0 {
		t.Errorf("不一致不应调用 XAdd，但调用了 %d 次", len(fc.Calls))
	}
}

func TestPublishRuntimeEventBadSchemaVersion(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	env := validRuntimeEventEnvelope()
	env.SchemaVersion = "9.9.9-wrong"

	err := tr.PublishRuntimeEvent(context.Background(), env)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
	if len(fc.Calls) != 0 {
		t.Error("bad schema_version 不应调用 XAdd")
	}
}

func TestPublishRuntimeEventXAddError(t *testing.T) {
	fc := newFakeClient()
	fc.XAddErr = errors.New("redis: stream full")
	tr := newTestTransport(fc)
	env := validRuntimeEventEnvelope()

	err := tr.PublishRuntimeEvent(context.Background(), env)
	if err == nil {
		t.Error("XAdd 返回 error 时应返回 error")
	}
}

// -- PublishWorkerHeartbeat 测试 --

func TestPublishWorkerHeartbeatSuccess(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	hb := validHeartbeat()

	err := tr.PublishWorkerHeartbeat(context.Background(), hb)
	if err != nil {
		t.Fatalf("PublishWorkerHeartbeat 失败: %v", err)
	}

	if len(fc.Calls) != 1 {
		t.Fatalf("期望 1 次 XAdd 调用，got %d", len(fc.Calls))
	}

	call := fc.Calls[0]
	if call.Stream != StreamWorkerHeartbeat {
		t.Errorf("stream: got %q, want %q", call.Stream, StreamWorkerHeartbeat)
	}

	vals := call.Values
	if vals[FieldSchemaVersion] != SchemaVersion {
		t.Errorf("schema_version 不正确")
	}
	if vals[FieldWorkerID] != "worker-01" {
		t.Errorf("worker_id: got %v", vals[FieldWorkerID])
	}
	if vals["status"] != "busy" {
		t.Errorf("status: got %v", vals["status"])
	}
	if vals["type"] != streamTypeWorkerHeartbeat {
		t.Errorf("type: got %v, want %q", vals["type"], streamTypeWorkerHeartbeat)
	}

	// heartbeat 不要求 trace_id
	if _, ok := vals[FieldTraceID]; ok {
		t.Error("heartbeat fields 不应包含 trace_id")
	}

	// payload JSON decode
	payloadStr, ok := vals[FieldPayload].(string)
	if !ok {
		t.Fatalf("payload 不是 string，got %T", vals[FieldPayload])
	}
	var decoded WorkerHeartbeatMessage
	if err := json.Unmarshal([]byte(payloadStr), &decoded); err != nil {
		t.Fatalf("payload JSON decode 失败: %v", err)
	}
	if decoded.WorkerID != hb.WorkerID {
		t.Errorf("payload WorkerID: got %q, want %q", decoded.WorkerID, hb.WorkerID)
	}
	if decoded.Status != hb.Status {
		t.Errorf("payload Status: got %q, want %q", decoded.Status, hb.Status)
	}
	if decoded.RuntimeBus == nil || decoded.RuntimeBus.Reclaimed != 2 {
		t.Errorf("payload RuntimeBus 未正确保留: %#v", decoded.RuntimeBus)
	}
}

func TestPublishWorkerHeartbeatIdle(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	hb := WorkerHeartbeatMessage{
		WorkerID:      "worker-02",
		Status:        "idle",
		ActiveRunID:   "",
		ReportedAt:    "2026-07-03T10:00:00Z",
		SchemaVersion: SchemaVersion,
	}

	err := tr.PublishWorkerHeartbeat(context.Background(), hb)
	if err != nil {
		t.Fatalf("idle heartbeat 失败: %v", err)
	}

	// 校验 payload 字段存在且为 string
	vals := fc.Calls[0].Values
	raw, ok := vals[FieldPayload]
	if !ok {
		t.Fatal("fields 缺少 payload")
	}
	payloadStr, ok := raw.(string)
	if !ok {
		t.Fatalf("payload 不是 string，got %T", raw)
	}

	var decoded WorkerHeartbeatMessage
	if err := json.Unmarshal([]byte(payloadStr), &decoded); err != nil {
		t.Fatalf("payload JSON decode 失败: %v", err)
	}
	if decoded.Status != "idle" {
		t.Errorf("payload Status: got %q, want %q", decoded.Status, "idle")
	}
	if decoded.WorkerID != "worker-02" {
		t.Errorf("payload WorkerID: got %q, want %q", decoded.WorkerID, "worker-02")
	}
}

func TestPublishMcpDiscoveryRefresh(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	cmd := McpDiscoveryRefreshCommand{
		CommandID: "mcp-cmd-1", TraceID: "trace-1",
		Type: "mcp.discovery.refresh", RequestedAt: "2026-07-26T12:00:00Z",
		SchemaVersion: SchemaVersion,
	}
	if err := tr.PublishMcpDiscoveryRefresh(context.Background(), cmd); err != nil {
		t.Fatalf("发布 MCP discovery 命令失败: %v", err)
	}
	if len(fc.Calls) != 1 || fc.Calls[0].Stream != StreamWorkerCommand {
		t.Fatalf("MCP discovery 必须写入 worker-command stream: %+v", fc.Calls)
	}
	values := fc.Calls[0].Values
	if values["type"] != "mcp.discovery.refresh" {
		t.Fatalf("command type = %v", values["type"])
	}
	if _, exists := values[FieldTaskID]; exists {
		t.Fatal("全局 MCP discovery 命令不得伪造 task_id")
	}
	if _, exists := values[FieldRunID]; exists {
		t.Fatal("全局 MCP discovery 命令不得伪造 run_id")
	}
}

func TestPublishWorkerHeartbeatMissingWorkerID(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	hb := validHeartbeat()
	hb.WorkerID = ""

	err := tr.PublishWorkerHeartbeat(context.Background(), hb)
	if err == nil {
		t.Error("缺 worker_id 应返回 error")
	}
	if len(fc.Calls) != 0 {
		t.Error("缺 worker_id 不应调用 XAdd")
	}
}

func TestPublishWorkerHeartbeatBadSchemaVersion(t *testing.T) {
	fc := newFakeClient()
	tr := newTestTransport(fc)
	hb := validHeartbeat()
	hb.SchemaVersion = "0.0.0-old"

	err := tr.PublishWorkerHeartbeat(context.Background(), hb)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
	if len(fc.Calls) != 0 {
		t.Error("bad schema_version 不应调用 XAdd")
	}
}

func TestPublishWorkerHeartbeatXAddError(t *testing.T) {
	fc := newFakeClient()
	fc.XAddErr = errors.New("redis: disconnected")
	tr := newTestTransport(fc)
	hb := validHeartbeat()

	err := tr.PublishWorkerHeartbeat(context.Background(), hb)
	if err == nil {
		t.Error("XAdd 返回 error 时应返回 error")
	}
	if len(fc.Calls) != 1 {
		t.Errorf("期望 1 次 XAdd 调用，got %d", len(fc.Calls))
	}
}

// -- fakeStreamClient 基础测试 --

func TestFakeStreamClientRecordsCalls(t *testing.T) {
	fc := newFakeClient()
	_ = fc.XAdd(context.Background(), "test:stream", map[string]interface{}{"k": "v"})
	_ = fc.XAdd(context.Background(), "test:stream2", map[string]interface{}{"a": "b"})

	if len(fc.Calls) != 2 {
		t.Fatalf("期望 2 次调用，got %d", len(fc.Calls))
	}
	if fc.Calls[0].Stream != "test:stream" {
		t.Errorf("第 1 次 stream: got %q", fc.Calls[0].Stream)
	}
}

func TestFakeStreamClientReset(t *testing.T) {
	fc := newFakeClient()
	_ = fc.XAdd(context.Background(), "s", map[string]interface{}{})
	fc.reset()

	if len(fc.Calls) != 0 {
		t.Errorf("reset 后 Calls 应为空，got %d", len(fc.Calls))
	}
	if fc.XAddErr != nil {
		t.Errorf("reset 后 XAddErr 应为 nil")
	}
}

// -- GoRedisStreamClient 编译期断言 --

func TestGoRedisStreamClientImplementsInterface(t *testing.T) {
	// 编译期断言在 go_redis_client.go：var _ RedisStreamClient = (*GoRedisStreamClient)(nil)
	// 此处验证构造函数不为 nil（可被引用）。
	_ = NewGoRedisStreamClient
}
