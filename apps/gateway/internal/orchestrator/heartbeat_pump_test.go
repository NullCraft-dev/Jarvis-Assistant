package orchestrator

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

func TestHeartbeatPumpStartClose(t *testing.T) {
	fakeReader := &runtimeredis.HeartbeatReader{} // 使用空 reader（测试中 fake reader 先创建后注入）
	// 由于 HeartbeatReader 需要 RedisStreamReader，这里测试泵的结构创建
	view := NewWorkerStatusView(DefaultStaleTimeout)
	backoff := newFakeBackoff()

	pump := newHeartbeatPump(fakeReader, view, backoff)
	if pump == nil {
		t.Fatal("newHeartbeatPump 不应返回 nil")
	}

	// Close 空 pump（无 Start）不 panic
	err := pump.Close()
	if err != nil {
		t.Errorf("Close 空 pump 不应返回 error: %v", err)
	}
}

// Fake 组件用于 pump 测试
type fakeHeartbeatStreamReader struct {
	XReadGroupFn func(ctx context.Context, group, consumer, stream, id string, count int64) ([]runtimeredis.StreamMessage, error)
	XAckFn       func(ctx context.Context, stream, group string, ids ...string) error
	XGroupFn     func(ctx context.Context, stream, group, startID string) error

	ReadCalls  int
	AckCalls   int
	GroupCalls int
	AckedIDs   []string
}

func (f *fakeHeartbeatStreamReader) XReadGroup(ctx context.Context, group, consumer, stream, id string, count int64) ([]runtimeredis.StreamMessage, error) {
	f.ReadCalls++
	if f.XReadGroupFn != nil {
		return f.XReadGroupFn(ctx, group, consumer, stream, id, count)
	}
	return nil, nil
}

func (f *fakeHeartbeatStreamReader) XAck(ctx context.Context, stream, group string, ids ...string) error {
	f.AckCalls++
	f.AckedIDs = append(f.AckedIDs, ids...)
	if f.XAckFn != nil {
		return f.XAckFn(ctx, stream, group, ids...)
	}
	return nil
}

func (f *fakeHeartbeatStreamReader) XGroupCreateMkStream(ctx context.Context, stream, group, startID string) error {
	f.GroupCalls++
	if f.XGroupFn != nil {
		return f.XGroupFn(ctx, stream, group, startID)
	}
	return nil
}

func TestHeartbeatPumpRecoversConsumerGroupAfterRedisStateLoss(t *testing.T) {
	fakeStream := &fakeHeartbeatStreamReader{}
	reader, err := runtimeredis.NewHeartbeatReader(fakeStream)
	if err != nil {
		t.Fatalf("创建 HeartbeatReader 失败: %v", err)
	}
	pump := newHeartbeatPump(
		reader, NewWorkerStatusView(DefaultStaleTimeout), newFakeBackoff(),
	)

	recovered, err := pump.recoverMissingConsumerGroup(
		context.Background(), fmt.Errorf("redis: NOGROUP stream was reset"),
	)
	if err != nil {
		t.Fatalf("重建 consumer group 失败: %v", err)
	}
	if !recovered || fakeStream.GroupCalls != 1 {
		t.Fatalf(
			"恢复结果错误: recovered=%v group_calls=%d",
			recovered, fakeStream.GroupCalls,
		)
	}
}

func TestHeartbeatPumpUpdatesView(t *testing.T) {
	fakeStream := &fakeHeartbeatStreamReader{
		// 首次调用返回 1 个心跳，之后返回空
		XReadGroupFn: func() func(ctx context.Context, group, consumer, stream, id string, count int64) ([]runtimeredis.StreamMessage, error) {
			callCount := 0
			return func(ctx context.Context, group, consumer, stream, id string, count int64) ([]runtimeredis.StreamMessage, error) {
				callCount++
				if callCount == 1 {
					hb := runtimeredis.WorkerHeartbeatMessage{
						WorkerID: "w1", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z", SchemaVersion: runtimeredis.SchemaVersion,
					}
					payload, _ := runtimeredis.WorkerHeartbeatToStreamFields(hb)
					return []runtimeredis.StreamMessage{
						{ID: "1000-0", Values: payload},
					}, nil
				}
				return nil, nil
			}
		}(),
	}

	reader, err := runtimeredis.NewHeartbeatReader(fakeStream)
	if err != nil {
		t.Fatalf("创建 HeartbeatReader 失败: %v", err)
	}

	view := NewWorkerStatusView(DefaultStaleTimeout)
	backoff := newFakeBackoff()
	pump := newHeartbeatPump(reader, view, backoff)

	err = pump.Start()
	if err != nil {
		t.Fatalf("Start 失败: %v", err)
	}

	// 等待 pump 处理
	time.Sleep(200 * time.Millisecond)

	err = pump.Close()
	if err != nil {
		t.Errorf("Close 失败: %v", err)
	}

	// 验证 view 中已有 worker
	if view.Count() != 1 {
		t.Errorf("Count = %d, want 1", view.Count())
	}
	ws, ok := view.Get("w1")
	if !ok {
		t.Fatal("view 中应有 w1")
	}
	if ws.Status != "idle" {
		t.Errorf("status = %q, want idle", ws.Status)
	}
}

func TestHeartbeatPumpMultipleWorkers(t *testing.T) {
	fakeStream := &fakeHeartbeatStreamReader{
		XReadGroupFn: func() func(ctx context.Context, group, consumer, stream, id string, count int64) ([]runtimeredis.StreamMessage, error) {
			callCount := 0
			return func(ctx context.Context, group, consumer, stream, id string, count int64) ([]runtimeredis.StreamMessage, error) {
				callCount++
				if callCount == 1 {
					// 返回 2 个不同 worker 的心跳
					hb1 := runtimeredis.WorkerHeartbeatMessage{
						WorkerID: "w1", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z", SchemaVersion: runtimeredis.SchemaVersion,
					}
					p1, _ := runtimeredis.WorkerHeartbeatToStreamFields(hb1)
					hb2 := runtimeredis.WorkerHeartbeatMessage{
						WorkerID: "w2", Status: "busy", ActiveRunID: "r1", ReportedAt: "2026-07-07T10:00:01Z", SchemaVersion: runtimeredis.SchemaVersion,
					}
					p2, _ := runtimeredis.WorkerHeartbeatToStreamFields(hb2)
					return []runtimeredis.StreamMessage{
						{ID: "1000-0", Values: p1},
						{ID: "1000-1", Values: p2},
					}, nil
				}
				return nil, nil
			}
		}(),
	}

	reader, _ := runtimeredis.NewHeartbeatReader(fakeStream)
	view := NewWorkerStatusView(DefaultStaleTimeout)
	backoff := newFakeBackoff()
	pump := newHeartbeatPump(reader, view, backoff)

	pump.Start()
	time.Sleep(200 * time.Millisecond)
	pump.Close()

	if view.Count() != 2 {
		t.Errorf("Count = %d, want 2", view.Count())
	}

	ws2, ok := view.Get("w2")
	if !ok {
		t.Fatal("view 中应有 w2")
	}
	if ws2.ActiveRunID != "r1" {
		t.Errorf("active_run_id = %q, want r1", ws2.ActiveRunID)
	}
}

func TestHeartbeatPumpSkipsInvalidHeartbeat(t *testing.T) {
	fakeStream := &fakeHeartbeatStreamReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]runtimeredis.StreamMessage, error) {
			return []runtimeredis.StreamMessage{
				{ID: "bad-msg", Values: map[string]interface{}{
					"schema_version": runtimeredis.SchemaVersion,
					// 缺少 payload → decode 失败
				}},
			}, nil
		},
	}

	reader, _ := runtimeredis.NewHeartbeatReader(fakeStream)
	view := NewWorkerStatusView(DefaultStaleTimeout)
	backoff := newFakeBackoff()
	pump := newHeartbeatPump(reader, view, backoff)

	// 不能真的 start（会死循环），直接测试 runOnce 返回 error
	_, err := pump.runOnce(context.Background())
	if err == nil {
		t.Error("非法 heartbeat 应返回 error")
	}

	// view 不应被污染
	if view.Count() != 0 {
		t.Errorf("非法 heartbeat 不应污染 status view: Count = %d", view.Count())
	}
}

func TestHeartbeatPumpSkipsInvalidStatus(t *testing.T) {
	// 即使 payload 完整但 status 非法，runOnce 也返回 error 且不更新 view
	raw := map[string]interface{}{
		"worker_id":      "worker-01",
		"status":         "sleeping", // 非法 status
		"reported_at":    "2026-07-07T10:00:00Z",
		"schema_version": runtimeredis.SchemaVersion,
	}
	payload, _ := json.Marshal(raw)

	fakeStream := &fakeHeartbeatStreamReader{
		XReadGroupFn: func(ctx context.Context, group, consumer, stream, id string, count int64) ([]runtimeredis.StreamMessage, error) {
			return []runtimeredis.StreamMessage{
				{ID: "bad-status-msg", Values: map[string]interface{}{
					runtimeredis.FieldSchemaVersion: runtimeredis.SchemaVersion,
					runtimeredis.FieldPayload:       string(payload),
				}},
			}, nil
		},
	}

	reader, _ := runtimeredis.NewHeartbeatReader(fakeStream)
	view := NewWorkerStatusView(DefaultStaleTimeout)
	backoff := newFakeBackoff()
	pump := newHeartbeatPump(reader, view, backoff)

	_, err := pump.runOnce(context.Background())
	if err == nil {
		t.Error("非法 status 应返回 error（整批不 ack）")
	}

	// view 不应被污染
	if view.Count() != 0 {
		t.Errorf("非法 status 不应污染 status view: Count = %d", view.Count())
	}
}

func TestHeartbeatPumpInmemoryModeNoRedis(t *testing.T) {
	// 验证 view 在无 Redis 时行为正常
	view := NewWorkerStatusView(DefaultStaleTimeout)

	// 无 pump 时 view 为空
	all := view.GetAll()
	if len(all) != 0 {
		t.Errorf("空 view GetAll len = %d, want 0", len(all))
	}
}

func newFakeBackoff() *fakeBackoffForHeartbeat {
	return &fakeBackoffForHeartbeat{}
}

type fakeBackoffForHeartbeat struct {
	resetCalls int
	waitCalls  int
}

func (f *fakeBackoffForHeartbeat) Reset() {
	f.resetCalls++
}

func (f *fakeBackoffForHeartbeat) Wait(ctx context.Context) error {
	f.waitCalls++
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(10 * time.Millisecond):
		return nil
	}
}
