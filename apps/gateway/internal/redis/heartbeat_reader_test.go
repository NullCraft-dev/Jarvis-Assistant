package redis

import (
	"context"
	"encoding/json"
	"testing"
)

// fakeHeartbeatReader 用于 heartbeat reader 测试的 fake RedisStreamReader。
type fakeHeartbeatReader struct {
	Messages     []StreamMessage
	XReadGroupFn func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error)
	XAckFn       func(ctx context.Context, stream, group string, ids ...string) error
	XGroupFn     func(ctx context.Context, stream, group, startID string) error

	AckedIDs         []string
	AckedStream      string
	AckedGroup       string
	CreateGroupCalls []struct {
		Stream  string
		Group   string
		StartID string
	}
}

func (f *fakeHeartbeatReader) XReadGroup(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
	if f.XReadGroupFn != nil {
		return f.XReadGroupFn(ctx, group, consumer, stream, id, count)
	}
	return nil, nil
}

func (f *fakeHeartbeatReader) XAck(ctx context.Context, stream, group string, ids ...string) error {
	f.AckedIDs = append(f.AckedIDs, ids...)
	f.AckedStream = stream
	f.AckedGroup = group
	if f.XAckFn != nil {
		return f.XAckFn(ctx, stream, group, ids...)
	}
	return nil
}

func (f *fakeHeartbeatReader) XGroupCreateMkStream(ctx context.Context, stream, group, startID string) error {
	f.CreateGroupCalls = append(f.CreateGroupCalls, struct {
		Stream  string
		Group   string
		StartID string
	}{stream, group, startID})
	if f.XGroupFn != nil {
		return f.XGroupFn(ctx, stream, group, startID)
	}
	return nil
}

func makeHeartbeatPayload(hb WorkerHeartbeatMessage) map[string]interface{} {
	payload, _ := json.Marshal(hb)
	m, _ := ToMap(hb)
	m[FieldPayload] = string(payload)
	return m
}

func TestNewHeartbeatReaderNilReader(t *testing.T) {
	_, err := NewHeartbeatReader(nil)
	if err == nil {
		t.Error("nil reader 应返回 error")
	}
}

func TestReadHeartbeatsSingleSuccess(t *testing.T) {
	hb := WorkerHeartbeatMessage{
		WorkerID:      "worker-01",
		Status:        "idle",
		ReportedAt:    "2026-07-07T10:00:00Z",
		SchemaVersion: SchemaVersion,
	}
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			return []StreamMessage{
				{ID: "1000-0", Values: makeHeartbeatPayload(hb)},
			}, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	hbs, ids, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err != nil {
		t.Fatalf("ReadHeartbeats 失败: %v", err)
	}
	if len(hbs) != 1 {
		t.Fatalf("期望 1 个 heartbeat，实际 %d", len(hbs))
	}
	if hbs[0].WorkerID != "worker-01" {
		t.Errorf("worker_id = %q, want worker-01", hbs[0].WorkerID)
	}
	if hbs[0].Status != "idle" {
		t.Errorf("status = %q, want idle", hbs[0].Status)
	}
	if len(ids) != 1 || ids[0] != "1000-0" {
		t.Errorf("msgIDs = %v, want [1000-0]", ids)
	}
}

func TestReadHeartbeatsMultipleSuccess(t *testing.T) {
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			return []StreamMessage{
				{ID: "1000-0", Values: makeHeartbeatPayload(WorkerHeartbeatMessage{
					WorkerID: "w1", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z", SchemaVersion: SchemaVersion,
				})},
				{ID: "1000-1", Values: makeHeartbeatPayload(WorkerHeartbeatMessage{
					WorkerID: "w2", Status: "busy", ActiveRunID: "r1", ReportedAt: "2026-07-07T10:00:01Z", SchemaVersion: SchemaVersion,
				})},
			}, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	hbs, ids, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err != nil {
		t.Fatalf("ReadHeartbeats 失败: %v", err)
	}
	if len(hbs) != 2 {
		t.Fatalf("期望 2 个 heartbeat，实际 %d", len(hbs))
	}
	if hbs[1].ActiveRunID != "r1" {
		t.Errorf("active_run_id = %q", hbs[1].ActiveRunID)
	}
	if len(ids) != 2 {
		t.Errorf("期望 2 个 msg id，实际 %d", len(ids))
	}
}

func TestReadHeartbeatsEmptyReturnsNil(t *testing.T) {
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			return nil, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	hbs, ids, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err != nil {
		t.Fatalf("空读取不应返回 error: %v", err)
	}
	if hbs != nil {
		t.Errorf("空读取应返回 nil envelopes，实际 %v", hbs)
	}
	if ids != nil {
		t.Errorf("空读取应返回 nil ids，实际 %v", ids)
	}
}

func TestReadHeartbeatsReaderError(t *testing.T) {
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			return nil, context.DeadlineExceeded
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	_, _, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err == nil {
		t.Error("reader error 应透传")
	}
}

func TestReadHeartbeatsMissingPayload(t *testing.T) {
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			return []StreamMessage{
				{ID: "1000-0", Values: map[string]interface{}{"type": "worker.heartbeat"}},
			}, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	_, _, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err == nil {
		t.Error("缺失 payload 应返回 error")
	}
}

func TestReadHeartbeatsPayloadNotString(t *testing.T) {
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			return []StreamMessage{
				{ID: "1000-0", Values: map[string]interface{}{
					FieldPayload: 12345, // 不是 string
				}},
			}, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	_, _, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err == nil {
		t.Error("payload 非 string 应返回 error")
	}
}

func TestReadHeartbeatsPayloadInvalidJSON(t *testing.T) {
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			return []StreamMessage{
				{ID: "1000-0", Values: map[string]interface{}{
					FieldPayload: "{not valid json",
				}},
			}, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	_, _, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err == nil {
		t.Error("无效 JSON 应返回 error")
	}
}

func TestReadHeartbeatsBadSchemaVersion(t *testing.T) {
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			return []StreamMessage{
				{ID: "1000-0", Values: makeHeartbeatPayload(WorkerHeartbeatMessage{
					WorkerID: "w1", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z", SchemaVersion: "bad-version",
				})},
			}, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	_, _, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err == nil {
		t.Error("bad schema_version 应返回 error")
	}
}

func TestReadHeartbeatsMissingWorkerID(t *testing.T) {
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			// payload JSON 中 worker_id 为空 → Decode 校验失败
			raw := map[string]interface{}{
				"worker_id":      "",
				"status":         "idle",
				"reported_at":    "2026-07-07T10:00:00Z",
				"schema_version": SchemaVersion,
			}
			payload, _ := json.Marshal(raw)
			return []StreamMessage{
				{ID: "1000-0", Values: map[string]interface{}{
					FieldSchemaVersion: SchemaVersion,
					FieldPayload:       string(payload),
				}},
			}, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	_, _, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err == nil {
		t.Error("缺失 worker_id 应返回 error")
	}
}

func TestReadHeartbeatsInvalidStatus(t *testing.T) {
	// "sleeping" 不是合法 worker status → Decode 失败
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			raw := map[string]interface{}{
				"worker_id":      "worker-01",
				"status":         "sleeping",
				"reported_at":    "2026-07-07T10:00:00Z",
				"schema_version": SchemaVersion,
			}
			payload, _ := json.Marshal(raw)
			return []StreamMessage{
				{ID: "1000-0", Values: map[string]interface{}{
					FieldSchemaVersion: SchemaVersion,
					FieldPayload:       string(payload),
				}},
			}, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	_, _, err := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	if err == nil {
		t.Error("非法 status 'sleeping' 应返回 error（批次解码失败）")
	}
}

func TestAckHeartbeatsSuccess(t *testing.T) {
	fake := &fakeHeartbeatReader{}
	reader, _ := NewHeartbeatReader(fake)

	err := reader.AckHeartbeats(context.Background(), "g", "1000-0", "1000-1")
	if err != nil {
		t.Fatalf("AckHeartbeats 失败: %v", err)
	}
	if len(fake.AckedIDs) != 2 {
		t.Errorf("期望 ack 2 个 id，实际 %d", len(fake.AckedIDs))
	}
	if fake.AckedStream != StreamWorkerHeartbeat {
		t.Errorf("ack stream = %q, want %q", fake.AckedStream, StreamWorkerHeartbeat)
	}
}

func TestAckHeartbeatsEmptyIDs(t *testing.T) {
	fake := &fakeHeartbeatReader{}
	reader, _ := NewHeartbeatReader(fake)

	err := reader.AckHeartbeats(context.Background(), "g")
	if err != nil {
		t.Fatalf("空 ids ack 不应返回 error: %v", err)
	}
}

func TestHeartbeatReadThenAckFullFlow(t *testing.T) {
	hb := WorkerHeartbeatMessage{
		WorkerID: "w1", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z", SchemaVersion: SchemaVersion,
	}
	fake := &fakeHeartbeatReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]StreamMessage, error) {
			return []StreamMessage{
				{ID: "1000-0", Values: makeHeartbeatPayload(hb)},
			}, nil
		},
	}

	reader, _ := NewHeartbeatReader(fake)
	_, ids, _ := reader.ReadHeartbeats(context.Background(), "g", "c", 32)
	reader.AckHeartbeats(context.Background(), "g", ids...)

	if len(fake.AckedIDs) != 1 || fake.AckedIDs[0] != "1000-0" {
		t.Errorf("ack 失败: acked=%v", fake.AckedIDs)
	}
}

func TestCreateGroupIfNotExists(t *testing.T) {
	fake := &fakeHeartbeatReader{}
	reader, _ := NewHeartbeatReader(fake)

	err := reader.CreateGroupIfNotExists(context.Background(), "my-group", "0")
	if err != nil {
		t.Fatalf("CreateGroupIfNotExists 失败: %v", err)
	}
	if len(fake.CreateGroupCalls) != 1 {
		t.Fatalf("期望 1 次调用，实际 %d", len(fake.CreateGroupCalls))
	}
	if fake.CreateGroupCalls[0].Stream != StreamWorkerHeartbeat {
		t.Errorf("stream = %q, want %q", fake.CreateGroupCalls[0].Stream, StreamWorkerHeartbeat)
	}
	if fake.CreateGroupCalls[0].Group != "my-group" {
		t.Errorf("group = %q", fake.CreateGroupCalls[0].Group)
	}
}
