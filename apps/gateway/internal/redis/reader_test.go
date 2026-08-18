package redis

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/contracts"
)

// -- fake stream reader for testing --

// fakeStreamReader 是 RedisStreamReader 的测试替身。
// 支持预设返回消息、注入读取错误和 ack 错误，记录 ack 调用。
type fakeStreamReader struct {
	// Messages 预设 XReadGroup 返回的消息列表
	Messages []StreamMessage
	// ReadErr 预设 XReadGroup 返回的错误
	ReadErr error
	// AckErr 预设 XAck 返回的错误
	AckErr error

	// AckedIDs 记录已 ack 的消息 id 列表
	AckedIDs []string
	// ReadCalls 记录 XReadGroup 调用次数
	ReadCalls int
	// AckCalls 记录 XAck 调用次数
	AckCalls int

	// LastStream 记录最后一次 XReadGroup 的 stream 参数
	LastStream string
	// LastGroup 记录最后一次 XReadGroup 的 group 参数
	LastGroup string
	// LastID 记录最后一次 XReadGroup 的 id 参数（如 ">" 表示仅新消息）
	LastID string
	// LastAckStream 记录最后一次 XAck 的 stream 参数
	LastAckStream string
	// LastAckGroup 记录最后一次 XAck 的 group 参数
	LastAckGroup string

	// CreateGroupErr 预设 XGroupCreateMkStream 返回的错误
	CreateGroupErr error
	// CreateGroupCalls 记录 XGroupCreateMkStream 调用次数
	CreateGroupCalls int
	// LastCreateStream/LastCreateGroup/LastCreateStartID 记录最后一次调用参数
	LastCreateStream  string
	LastCreateGroup   string
	LastCreateStartID string
}

// 确保 fakeStreamReader 实现 RedisStreamReader 接口。
var _ RedisStreamReader = (*fakeStreamReader)(nil)

func (f *fakeStreamReader) XReadGroup(_ context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
	f.ReadCalls++
	f.LastStream = stream
	f.LastGroup = group
	f.LastID = id
	if f.ReadErr != nil {
		return nil, f.ReadErr
	}
	return f.Messages, nil
}

func (f *fakeStreamReader) XAck(_ context.Context, stream, group string, ids ...string) error {
	f.AckCalls++
	f.LastAckStream = stream
	f.LastAckGroup = group
	f.AckedIDs = append(f.AckedIDs, ids...)
	return f.AckErr
}

func (f *fakeStreamReader) XGroupCreateMkStream(_ context.Context, stream, group, startID string) error {
	f.CreateGroupCalls++
	f.LastCreateStream = stream
	f.LastCreateGroup = group
	f.LastCreateStartID = startID
	return f.CreateGroupErr
}

// reset 清空所有记录（测试 helper）。
func (f *fakeStreamReader) reset() {
	f.Messages = nil
	f.ReadErr = nil
	f.AckErr = nil
	f.AckedIDs = nil
	f.ReadCalls = 0
	f.AckCalls = 0
	f.LastStream = ""
	f.LastGroup = ""
	f.LastID = ""
	f.LastAckStream = ""
	f.LastAckGroup = ""
	f.CreateGroupErr = nil
	f.CreateGroupCalls = 0
	f.LastCreateStream = ""
	f.LastCreateGroup = ""
	f.LastCreateStartID = ""
}

// -- helpers --

func newFakeReader() *fakeStreamReader {
	return &fakeStreamReader{}
}

func newTestEventReader(fr *fakeStreamReader) *RuntimeEventReader {
	r, err := NewRuntimeEventReader(fr)
	if err != nil {
		panic(fmt.Sprintf("newTestEventReader: %v", err))
	}
	return r
}

// makeValidStreamMsg 使用 RuntimeEventToStreamFields 构造一个合法的 StreamMessage。
// 消息 id 前缀为 "msg"，编号从 1 开始。
func makeValidStreamMsg(seq int) StreamMessage {
	env := RuntimeEventEnvelope{
		EventID:   dtoID(fmt.Sprintf("evt-%03d", seq)),
		TraceID:   dtoID(fmt.Sprintf("trace-%03d", seq)),
		TaskID:    dtoID(fmt.Sprintf("task-%03d", seq)),
		RunID:     dtoID(fmt.Sprintf("run-%03d", seq)),
		EventType: "agent.run.completed",
		RuntimeEvent: dtoRuntimeEvent(
			dtoID(fmt.Sprintf("evt-%03d", seq)),
			"agent.run.completed",
			dtoID(fmt.Sprintf("task-%03d", seq)),
			dtoID(fmt.Sprintf("run-%03d", seq)),
			"step-001",
			"2026-07-06T10:00:00Z",
			map[string]interface{}{
				"output": "任务完成",
			},
		),
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
	}

	fields, err := RuntimeEventToStreamFields(env)
	if err != nil {
		panic(fmt.Sprintf("makeValidStreamMsg: RuntimeEventToStreamFields: %v", err))
	}
	return StreamMessage{
		ID:     fmt.Sprintf("1700000000000-%d", seq),
		Values: fields,
	}
}

// makeValidStreamMsgs 构造 n 个合法 StreamMessage。
func makeValidStreamMsgs(n int) []StreamMessage {
	msgs := make([]StreamMessage, n)
	for i := 0; i < n; i++ {
		msgs[i] = makeValidStreamMsg(i + 1)
	}
	return msgs
}

// dtoID helper（避免重复写 contracts.ID(...)）。
func dtoID(s string) string {
	return s
}

// dtoRuntimeEvent 构造一个合法的 contracts.RuntimeEvent。
func dtoRuntimeEvent(id, eventType, taskID, runID, stepID, timestamp string, payload map[string]interface{}) contracts.RuntimeEvent {
	return contracts.RuntimeEvent{
		ID:        id,
		Type:      eventType,
		TaskID:    taskID,
		RunID:     runID,
		StepID:    stepID,
		Timestamp: timestamp,
		Payload:   payload,
	}
}

// -- nil guard 测试 --

func TestNewRuntimeEventReaderNilReader(t *testing.T) {
	r, err := NewRuntimeEventReader(nil)
	if err == nil {
		t.Error("nil RedisStreamReader 应返回 error")
	}
	if r != nil {
		t.Error("nil reader 时 RuntimeEventReader 应为 nil")
	}
}

func TestNewGoRedisStreamReaderNilClient(t *testing.T) {
	gr, err := NewGoRedisStreamReader(nil)
	if err == nil {
		t.Error("nil *redis.Client 应返回 error")
	}
	if gr != nil {
		t.Error("nil client 时 GoRedisStreamReader 应为 nil")
	}
}

func TestReadDeliveriesIsolatesMalformedSibling(t *testing.T) {
	fr := newFakeReader()
	valid := makeValidStreamMsg(1)
	fr.Messages = []StreamMessage{
		{ID: "poison-1", Values: map[string]interface{}{
			FieldSchemaVersion: SchemaVersion,
			FieldPayload:       "{not-json",
			FieldEventID:       "bad-event",
			FieldTraceID:       "bad-trace",
			FieldTaskID:        "bad-task",
			FieldRunID:         "bad-run",
			"type":             "agent.run.completed",
		}},
		valid,
	}
	reader := newTestEventReader(fr)

	deliveries, err := reader.ReadDeliveries(
		context.Background(), GroupGatewayEvents, "gateway-test", 10,
	)

	if err != nil || len(deliveries) != 2 {
		t.Fatalf("ReadDeliveries: len=%d err=%v", len(deliveries), err)
	}
	if deliveries[0].Valid() || deliveries[0].ErrorCode != "RUNTIME_EVENT_MALFORMED" {
		t.Fatalf("poison event 未隔离: %#v", deliveries[0])
	}
	if !deliveries[1].Valid() {
		t.Fatalf("正常 sibling 不应受影响: %#v", deliveries[1])
	}
}

func TestReadDeliveriesRejectsOuterRoutingMismatch(t *testing.T) {
	fr := newFakeReader()
	message := makeValidStreamMsg(1)
	message.Values[FieldRunID] = "outer-wrong-run"
	fr.Messages = []StreamMessage{message}
	reader := newTestEventReader(fr)

	deliveries, err := reader.ReadDeliveries(
		context.Background(), GroupGatewayEvents, "gateway-test", 1,
	)

	if err != nil || len(deliveries) != 1 {
		t.Fatalf("ReadDeliveries: %#v err=%v", deliveries, err)
	}
	if deliveries[0].ErrorCode != "RUNTIME_EVENT_ROUTING_MISMATCH" {
		t.Fatalf("routing mismatch 未拒绝: %#v", deliveries[0])
	}
}

// -- ReadEvents 成功场景 --

func TestReadEventsSingleSuccess(t *testing.T) {
	fr := newFakeReader()
	fr.Messages = makeValidStreamMsgs(1)
	er := newTestEventReader(fr)

	envs, ids, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err != nil {
		t.Fatalf("ReadEvents 失败: %v", err)
	}
	if len(envs) != 1 {
		t.Fatalf("期望 1 个 envelope，got %d", len(envs))
	}
	if len(ids) != 1 {
		t.Fatalf("期望 1 个 msg id，got %d", len(ids))
	}

	env := envs[0]
	if env.EventID != "evt-001" {
		t.Errorf("EventID: got %q, want %q", env.EventID, "evt-001")
	}
	if env.TraceID != "trace-001" {
		t.Errorf("TraceID: got %q, want %q", env.TraceID, "trace-001")
	}
	if env.EventType != "agent.run.completed" {
		t.Errorf("EventType: got %q, want %q", env.EventType, "agent.run.completed")
	}
	if env.RuntimeEvent.ID != "evt-001" {
		t.Errorf("RuntimeEvent.ID: got %q, want %q", env.RuntimeEvent.ID, "evt-001")
	}
	if ids[0] != "1700000000000-1" {
		t.Errorf("msg id: got %q, want %q", ids[0], "1700000000000-1")
	}

	// 验证读取了正确的 stream
	if fr.LastStream != StreamRuntimeEvent {
		t.Errorf("读取的 stream: got %q, want %q", fr.LastStream, StreamRuntimeEvent)
	}
}

func TestReadEventsMultipleSuccess(t *testing.T) {
	fr := newFakeReader()
	fr.Messages = makeValidStreamMsgs(3)
	er := newTestEventReader(fr)

	envs, ids, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err != nil {
		t.Fatalf("ReadEvents 失败: %v", err)
	}
	if len(envs) != 3 {
		t.Fatalf("期望 3 个 envelope，got %d", len(envs))
	}
	if len(ids) != 3 {
		t.Fatalf("期望 3 个 msg id，got %d", len(ids))
	}

	for i, env := range envs {
		expectedSeq := i + 1
		expectedEventID := fmt.Sprintf("evt-%03d", expectedSeq)
		if env.EventID != expectedEventID {
			t.Errorf("env[%d].EventID: got %q, want %q", i, env.EventID, expectedEventID)
		}
		expectedMsgID := fmt.Sprintf("1700000000000-%d", expectedSeq)
		if ids[i] != expectedMsgID {
			t.Errorf("ids[%d]: got %q, want %q", i, ids[i], expectedMsgID)
		}
	}
}

func TestReadEventsEmptyReturnsNil(t *testing.T) {
	fr := newFakeReader()
	fr.Messages = nil // 空列表
	er := newTestEventReader(fr)

	envs, ids, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err != nil {
		t.Fatalf("空读取应不报错，got: %v", err)
	}
	if envs != nil {
		t.Errorf("空读取 envelopes 应为 nil，got %v", envs)
	}
	if ids != nil {
		t.Errorf("空读取 ids 应为 nil，got %v", ids)
	}
}

func TestReadEventsReaderError(t *testing.T) {
	fr := newFakeReader()
	fr.ReadErr = errors.New("redis: connection refused")
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err == nil {
		t.Error("XReadGroup 返回 error 时应返回 error")
	}
}

// -- 解码失败场景（均不 ack） --

func TestReadEventsMissingPayload(t *testing.T) {
	fr := newFakeReader()
	// 构造缺少 payload 字段的消息
	fr.Messages = []StreamMessage{{
		ID: "1700000000000-1",
		Values: map[string]interface{}{
			FieldSchemaVersion: SchemaVersion,
			FieldEventID:       "env-001",
			FieldTraceID:       "trace-001",
			"type":             "agent.run.completed",
		},
	}}
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err == nil {
		t.Error("payload 缺失应返回 error")
	}
	// 解码失败不应 ack
	if fr.AckCalls > 0 {
		t.Errorf("payload 缺失不应调用 ack，但调用了 %d 次", fr.AckCalls)
	}
}

func TestReadEventsPayloadNotString(t *testing.T) {
	fr := newFakeReader()
	fr.Messages = []StreamMessage{{
		ID: "1700000000000-1",
		Values: map[string]interface{}{
			FieldSchemaVersion: SchemaVersion,
			FieldPayload:       12345, // 不是 string
			FieldEventID:       "env-001",
			FieldTraceID:       "trace-001",
			"type":             "agent.run.completed",
		},
	}}
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err == nil {
		t.Error("payload 非 string 应返回 error")
	}
	if fr.AckCalls > 0 {
		t.Errorf("payload 非 string 不应调用 ack，但调用了 %d 次", fr.AckCalls)
	}
}

func TestReadEventsPayloadInvalidJSON(t *testing.T) {
	fr := newFakeReader()
	fr.Messages = []StreamMessage{{
		ID: "1700000000000-1",
		Values: map[string]interface{}{
			FieldSchemaVersion: SchemaVersion,
			FieldPayload:       "{not valid json",
			FieldEventID:       "env-001",
			FieldTraceID:       "trace-001",
			"type":             "agent.run.completed",
		},
	}}
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err == nil {
		t.Error("payload JSON 无效应返回 error")
	}
	if fr.AckCalls > 0 {
		t.Errorf("payload JSON 无效不应调用 ack，但调用了 %d 次", fr.AckCalls)
	}
}

func TestReadEventsBadSchemaVersion(t *testing.T) {
	fr := newFakeReader()
	// 构造 schema_version 不匹配的合法 JSON payload
	badEnv := RuntimeEventEnvelope{
		EventID:       "evt-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "agent.run.completed",
		ProducedBy:    "worker-01",
		SchemaVersion: "9.9.9-wrong", // 错误版本
		RuntimeEvent: dtoRuntimeEvent(
			"evt-001", "agent.run.completed", "task-001", "run-001",
			"step-001", "2026-07-06T10:00:00Z",
			map[string]interface{}{"output": "done"},
		),
	}
	payloadBytes, _ := json.Marshal(badEnv)
	fr.Messages = []StreamMessage{{
		ID: "1700000000000-1",
		Values: map[string]interface{}{
			FieldSchemaVersion: SchemaVersion,
			FieldPayload:       string(payloadBytes),
		},
	}}
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err == nil {
		t.Error("schema_version 错误应返回 error")
	}
	if fr.AckCalls > 0 {
		t.Errorf("schema_version 错误不应调用 ack，但调用了 %d 次", fr.AckCalls)
	}
}

func TestReadEventsEventTypeMismatch(t *testing.T) {
	fr := newFakeReader()
	// 构造 event_type 与 runtime_event.type 不一致的 payload
	badEnv := RuntimeEventEnvelope{
		EventID:       "evt-001",
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "agent.run.completed", // envelope 层
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
		RuntimeEvent: dtoRuntimeEvent(
			"evt-001", "tool.call.started", // 内层不一致！
			"task-001", "run-001",
			"step-001", "2026-07-06T10:00:00Z",
			map[string]interface{}{"output": "done"},
		),
	}
	payloadBytes, _ := json.Marshal(badEnv)
	fr.Messages = []StreamMessage{{
		ID: "1700000000000-1",
		Values: map[string]interface{}{
			FieldSchemaVersion: SchemaVersion,
			FieldPayload:       string(payloadBytes),
		},
	}}
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err == nil {
		t.Error("event_type 不一致应返回 error")
	}
	if fr.AckCalls > 0 {
		t.Errorf("event_type 不一致不应调用 ack，但调用了 %d 次", fr.AckCalls)
	}
}

func TestReadEventsDecodeFailureDoesNotAckOthers(t *testing.T) {
	// 多个消息中第 2 个解码失败，不应 ack 第 1 个
	fr := newFakeReader()
	msgs := makeValidStreamMsgs(1)

	// 第 2 个消息 payload 缺失
	badMsg := StreamMessage{
		ID: "1700000000000-2",
		Values: map[string]interface{}{
			FieldSchemaVersion: SchemaVersion,
			// 无 payload 字段
		},
	}
	fr.Messages = append(msgs, badMsg)
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err == nil {
		t.Error("第 2 个消息解码失败应返回 error")
	}
	// 任何消息都不应被 ack（包括第 1 个）
	if fr.AckCalls > 0 {
		t.Errorf("解码失败不应调用 ack，但调用了 %d 次", fr.AckCalls)
	}
}

func TestReadEventsMissingTraceID(t *testing.T) {
	fr := newFakeReader()
	// 构造 trace_id 缺失的 payload
	badEnv := RuntimeEventEnvelope{
		EventID:       "evt-001",
		TraceID:       "", // 缺失 trace_id
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "agent.run.completed",
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
		RuntimeEvent: dtoRuntimeEvent(
			"evt-001", "agent.run.completed", "task-001", "run-001",
			"step-001", "2026-07-06T10:00:00Z",
			map[string]interface{}{"output": "done"},
		),
	}
	payloadBytes, _ := json.Marshal(badEnv)
	fr.Messages = []StreamMessage{{
		ID: "1700000000000-1",
		Values: map[string]interface{}{
			FieldSchemaVersion: SchemaVersion,
			FieldPayload:       string(payloadBytes),
		},
	}}
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err == nil {
		t.Error("缺 trace_id 应返回 error")
	}
	if fr.AckCalls > 0 {
		t.Errorf("缺 trace_id 不应调用 ack，但调用了 %d 次", fr.AckCalls)
	}
}

func TestReadEventsMissingEventID(t *testing.T) {
	fr := newFakeReader()
	badEnv := RuntimeEventEnvelope{
		EventID:       "", // 缺失 event_id
		TraceID:       "trace-001",
		TaskID:        "task-001",
		RunID:         "run-001",
		EventType:     "agent.run.completed",
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
		RuntimeEvent: dtoRuntimeEvent(
			"evt-001", "agent.run.completed", "task-001", "run-001",
			"step-001", "2026-07-06T10:00:00Z",
			map[string]interface{}{"output": "done"},
		),
	}
	payloadBytes, _ := json.Marshal(badEnv)
	fr.Messages = []StreamMessage{{
		ID: "1700000000000-1",
		Values: map[string]interface{}{
			FieldSchemaVersion: SchemaVersion,
			FieldPayload:       string(payloadBytes),
		},
	}}
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err == nil {
		t.Error("缺 event_id 应返回 error")
	}
	if fr.AckCalls > 0 {
		t.Errorf("缺 event_id 不应调用 ack，但调用了 %d 次", fr.AckCalls)
	}
}

// -- Ack 测试 --

func TestAckEventsSuccess(t *testing.T) {
	fr := newFakeReader()
	er := newTestEventReader(fr)

	err := er.AckEvents(context.Background(), GroupGatewayEvents, "1700000000000-1", "1700000000000-2")
	if err != nil {
		t.Fatalf("AckEvents 失败: %v", err)
	}
	if fr.AckCalls != 1 {
		t.Errorf("期望 1 次 XAck 调用，got %d", fr.AckCalls)
	}
	if len(fr.AckedIDs) != 2 {
		t.Errorf("期望 2 个 ack id，got %d", len(fr.AckedIDs))
	}
	if fr.AckedIDs[0] != "1700000000000-1" {
		t.Errorf("AckedIDs[0]: got %q", fr.AckedIDs[0])
	}
	if fr.AckedIDs[1] != "1700000000000-2" {
		t.Errorf("AckedIDs[1]: got %q", fr.AckedIDs[1])
	}
}

func TestAckEventsEmptyIDs(t *testing.T) {
	fr := newFakeReader()
	er := newTestEventReader(fr)

	err := er.AckEvents(context.Background(), GroupGatewayEvents)
	if err != nil {
		t.Fatalf("空 ids 的 AckEvents 应返回 nil，got: %v", err)
	}
	if fr.AckCalls > 0 {
		t.Errorf("空 ids 不应调用 XAck，但调用了 %d 次", fr.AckCalls)
	}
}

func TestAckEventsClientError(t *testing.T) {
	fr := newFakeReader()
	fr.AckErr = errors.New("redis: ack failed")
	er := newTestEventReader(fr)

	err := er.AckEvents(context.Background(), GroupGatewayEvents, "1700000000000-1")
	if err == nil {
		t.Error("XAck 返回 error 时应返回 error")
	}
}

func TestAckEventsUsesCorrectStreamAndGroup(t *testing.T) {
	fr := newFakeReader()
	er := newTestEventReader(fr)

	_ = er.AckEvents(context.Background(), GroupGatewayEvents, "1700000000000-1")
	if fr.AckCalls != 1 {
		t.Fatalf("期望 1 次 XAck，got %d", fr.AckCalls)
	}
	if fr.LastAckStream != StreamRuntimeEvent {
		t.Errorf("XAck stream: got %q, want %q", fr.LastAckStream, StreamRuntimeEvent)
	}
	if fr.LastAckGroup != GroupGatewayEvents {
		t.Errorf("XAck group: got %q, want %q", fr.LastAckGroup, GroupGatewayEvents)
	}
}

func TestReadEventsUsesCorrectStreamAndGroup(t *testing.T) {
	fr := newFakeReader()
	fr.Messages = makeValidStreamMsgs(1)
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err != nil {
		t.Fatalf("ReadEvents 失败: %v", err)
	}
	if fr.LastStream != StreamRuntimeEvent {
		t.Errorf("LastStream: got %q, want %q", fr.LastStream, StreamRuntimeEvent)
	}
	if fr.LastGroup != GroupGatewayEvents {
		t.Errorf("LastGroup: got %q, want %q", fr.LastGroup, GroupGatewayEvents)
	}
	// XReadGroup id 固定为 ">"（仅新消息，与非阻塞读取兼容）
	if fr.LastID != ">" {
		t.Errorf("LastID: got %q, want %q", fr.LastID, ">")
	}
}

// TestReadEventsNonBlockingID 验证 ReadEvents 始终使用 id=">"（仅新消息），
// 不会发送 BLOCK 0（无限阻塞）。XReadGroup id=">" 只消费新消息，空 stream 时立即返回空列表。
func TestReadEventsNonBlockingID(t *testing.T) {
	fr := newFakeReader()
	fr.Messages = nil // 空 stream 模拟
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err != nil {
		t.Fatalf("空读取应不报错: %v", err)
	}
	// 验证使用了非阻塞 id ">"
	if fr.LastID != ">" {
		t.Errorf("非阻塞读取 id 应为 \">\"，got %q", fr.LastID)
	}
	// 验证只调用了 1 次 XReadGroup（非阻塞，不重试）
	if fr.ReadCalls != 1 {
		t.Errorf("非阻塞读取应只调用 1 次 XReadGroup，got %d", fr.ReadCalls)
	}
}

// TestReadEventsNonBlockingIDPresence 验证有消息时也使用 id=">"。
func TestReadEventsNonBlockingIDPresence(t *testing.T) {
	fr := newFakeReader()
	fr.Messages = makeValidStreamMsgs(1)
	er := newTestEventReader(fr)

	_, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err != nil {
		t.Fatalf("ReadEvents 失败: %v", err)
	}
	if fr.LastID != ">" {
		t.Errorf("有消息时 id 也应为 \">\"，got %q", fr.LastID)
	}
}

// -- end-to-end: Read + Ack 正常流程 --

func TestReadThenAckFullFlow(t *testing.T) {
	fr := newFakeReader()
	fr.Messages = makeValidStreamMsgs(2)
	er := newTestEventReader(fr)

	// 1. 读取
	envs, ids, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err != nil {
		t.Fatalf("ReadEvents 失败: %v", err)
	}
	if len(envs) != 2 {
		t.Fatalf("期望 2 个 envelope，got %d", len(envs))
	}

	// 2. 处理成功，ack
	err = er.AckEvents(context.Background(), GroupGatewayEvents, ids...)
	if err != nil {
		t.Fatalf("AckEvents 失败: %v", err)
	}

	if len(fr.AckedIDs) != 2 {
		t.Errorf("期望 ack 2 个 id，got %d", len(fr.AckedIDs))
	}
}

// -- GoRedisStreamReader 编译期断言 --

func TestGoRedisStreamReaderImplementsInterface(t *testing.T) {
	// 编译期断言在 go_redis_reader.go：var _ RedisStreamReader = (*GoRedisStreamReader)(nil)
	// 此处验证构造函数不为 nil（可被引用）。
	_ = NewGoRedisStreamReader
}

// -- RuntimeEventEnvelope 内层 RuntimeEvent 字段完整保留 --

func TestReadEventsPreservesRuntimeEventPayload(t *testing.T) {
	fr := newFakeReader()
	// 构造一个包含复杂 payload 的 envelope
	env := RuntimeEventEnvelope{
		EventID:   "evt-payload",
		TraceID:   "trace-payload",
		TaskID:    "task-payload",
		RunID:     "run-payload",
		EventType: "tool.call.finished",
		RuntimeEvent: dtoRuntimeEvent(
			"evt-payload",
			"tool.call.finished",
			"task-payload",
			"run-payload",
			"step-payload",
			"2026-07-06T10:00:00Z",
			map[string]interface{}{
				"tool_call": map[string]interface{}{
					"id":        "tc-001",
					"tool_name": "read_file",
					"provider":  "native",
				},
			},
		),
		ProducedBy:    "worker-01",
		SchemaVersion: SchemaVersion,
	}

	fields, err := RuntimeEventToStreamFields(env)
	if err != nil {
		t.Fatalf("RuntimeEventToStreamFields 失败: %v", err)
	}
	fr.Messages = []StreamMessage{{ID: "1700000000000-1", Values: fields}}
	er := newTestEventReader(fr)

	envs, _, err := er.ReadEvents(context.Background(), GroupGatewayEvents, "gateway-01", 10)
	if err != nil {
		t.Fatalf("ReadEvents 失败: %v", err)
	}
	if len(envs) != 1 {
		t.Fatalf("期望 1 个 envelope，got %d", len(envs))
	}

	re := envs[0].RuntimeEvent
	if re.ID != "evt-payload" {
		t.Errorf("RuntimeEvent.ID: got %q", re.ID)
	}
	if re.Type != "tool.call.finished" {
		t.Errorf("RuntimeEvent.Type: got %q", re.Type)
	}
	// 验证嵌套 payload 保留
	tc, ok := re.Payload["tool_call"].(map[string]interface{})
	if !ok {
		t.Fatal("RuntimeEvent.Payload.tool_call 不是 map")
	}
	if tc["tool_name"] != "read_file" {
		t.Errorf("tool_call.tool_name: got %v, want %q", tc["tool_name"], "read_file")
	}
	if tc["provider"] != "native" {
		t.Errorf("tool_call.provider: got %v, want %q", tc["provider"], "native")
	}
}

// -- XGroupCreateMkStream 测试 --

func TestXGroupCreateMkStreamSuccess(t *testing.T) {
	fr := newFakeReader()

	err := fr.XGroupCreateMkStream(context.Background(), StreamRuntimeEvent, GroupGatewayEvents, "0")
	if err != nil {
		t.Fatalf("XGroupCreateMkStream 失败: %v", err)
	}
	if fr.CreateGroupCalls != 1 {
		t.Errorf("期望 1 次调用，got %d", fr.CreateGroupCalls)
	}
	if fr.LastCreateStream != StreamRuntimeEvent {
		t.Errorf("stream: got %q, want %q", fr.LastCreateStream, StreamRuntimeEvent)
	}
	if fr.LastCreateGroup != GroupGatewayEvents {
		t.Errorf("group: got %q, want %q", fr.LastCreateGroup, GroupGatewayEvents)
	}
	if fr.LastCreateStartID != "0" {
		t.Errorf("startID: got %q, want %q", fr.LastCreateStartID, "0")
	}
}

func TestXGroupCreateMkStreamError(t *testing.T) {
	fr := newFakeReader()
	fr.CreateGroupErr = errors.New("redis: connection refused")

	err := fr.XGroupCreateMkStream(context.Background(), StreamRuntimeEvent, GroupGatewayEvents, "0")
	if err == nil {
		t.Error("非 BUSYGROUP 错误应返回 error")
	}
}

func TestXGroupCreateMkStreamResetClearsFields(t *testing.T) {
	fr := newFakeReader()
	fr.CreateGroupErr = errors.New("some err")
	fr.CreateGroupCalls = 5
	fr.LastCreateStream = "x"
	fr.LastCreateGroup = "y"
	fr.LastCreateStartID = "$"

	fr.reset()
	if fr.CreateGroupErr != nil {
		t.Error("reset 后 CreateGroupErr 应为 nil")
	}
	if fr.CreateGroupCalls != 0 {
		t.Errorf("reset 后 CreateGroupCalls 应为 0，got %d", fr.CreateGroupCalls)
	}
	if fr.LastCreateStream != "" {
		t.Error("reset 后 LastCreateStream 应为空")
	}
}
