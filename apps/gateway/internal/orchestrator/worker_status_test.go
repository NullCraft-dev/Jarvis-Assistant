package orchestrator

import (
	"testing"
	"time"

	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

func TestNewWorkerStatusViewDefaultTimeout(t *testing.T) {
	v := NewWorkerStatusView(0)
	if v.staleTimeout != DefaultStaleTimeout {
		t.Errorf("默认 staleTimeout = %v, want %v", v.staleTimeout, DefaultStaleTimeout)
	}
}

func TestNewWorkerStatusViewCustomTimeout(t *testing.T) {
	custom := 5 * time.Second
	v := NewWorkerStatusView(custom)
	if v.staleTimeout != custom {
		t.Errorf("staleTimeout = %v, want %v", v.staleTimeout, custom)
	}
}

func TestUpdateFromHeartbeat(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	hb := runtimeredis.WorkerHeartbeatMessage{
		WorkerID:   "worker-01",
		Status:     "idle",
		ReportedAt: "2026-07-07T10:00:00Z",
	}
	v.UpdateFromHeartbeat(hb)

	ws, ok := v.Get("worker-01")
	if !ok {
		t.Fatal("worker-01 应存在")
	}
	if ws.WorkerID != "worker-01" {
		t.Errorf("worker_id = %q", ws.WorkerID)
	}
	if ws.WorkerKind != "agent" {
		t.Errorf("旧 heartbeat 的 worker_kind = %q, want agent", ws.WorkerKind)
	}
	if ws.Status != "idle" {
		t.Errorf("status = %q", ws.Status)
	}
	if ws.ReportedAt != "2026-07-07T10:00:00Z" {
		t.Errorf("reported_at = %q", ws.ReportedAt)
	}
	if ws.LastSeenAt == "" {
		t.Error("last_seen_at 不应为空")
	}
	if ws.IsStale {
		t.Error("刚更新的 worker 不应为 stale")
	}
}

func TestUpdateFromRagWorkerHeartbeat(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "rag-worker-01", WorkerKind: "rag", Status: "busy",
		ReportedAt: "2026-07-07T10:00:00Z",
	})

	ws, ok := v.Get("rag-worker-01")
	if !ok || ws.WorkerKind != "rag" || ws.ActiveRunID != "" {
		t.Fatalf("RAG Worker 投影错误: %#v", ws)
	}
}

func TestUpdateFromHeartbeatActiveRunID(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	hb := runtimeredis.WorkerHeartbeatMessage{
		WorkerID:    "worker-01",
		Status:      "busy",
		ActiveRunID: "run-123",
		ReportedAt:  "2026-07-07T10:00:00Z",
	}
	v.UpdateFromHeartbeat(hb)

	ws, _ := v.Get("worker-01")
	if ws.ActiveRunID != "run-123" {
		t.Errorf("active_run_id = %q, want run-123", ws.ActiveRunID)
	}
}

func TestUpdateFromHeartbeatRuntimeBusMetrics(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID:   "worker-01",
		Status:     "idle",
		ReportedAt: "2026-07-07T10:00:00Z",
		RuntimeBus: &runtimeredis.WorkerRuntimeBusMetrics{
			Reclaimed: 4, RetryDeferred: 2, DeadLettered: 1, Malformed: 1,
			CommandReclaimed: 2, CommandDeadLettered: 1, CommandMalformed: 1,
		},
	})

	ws, _ := v.Get("worker-01")
	if ws.RuntimeBus == nil || ws.RuntimeBus.Reclaimed != 4 || ws.RuntimeBus.DeadLettered != 1 {
		t.Fatalf("runtime_bus metrics 未正确投影: %#v", ws.RuntimeBus)
	}
	if ws.RuntimeBus.CommandReclaimed != 2 || ws.RuntimeBus.CommandDeadLettered != 1 {
		t.Fatalf("command runtime_bus metrics 未正确投影: %#v", ws.RuntimeBus)
	}
}

func TestUpdateFromHeartbeatOverwritesPrevious(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)

	// 第一次更新
	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "worker-01", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z",
	})
	ws1, _ := v.Get("worker-01")
	if ws1.Status != "idle" {
		t.Errorf("status = %q, want idle", ws1.Status)
	}
	if ws1.LastSeenAt == "" {
		t.Fatal("last_seen_at 不应为空")
	}

	// 等待一小段时间确保时间戳不同
	time.Sleep(10 * time.Millisecond)

	// 第二次更新：状态和 active_run_id 应覆盖
	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "worker-01", Status: "busy", ActiveRunID: "run-1", ReportedAt: "2026-07-07T10:00:01Z",
	})
	ws2, _ := v.Get("worker-01")

	if ws2.Status != "busy" {
		t.Errorf("status = %q, want busy (应覆盖)", ws2.Status)
	}
	if ws2.ActiveRunID != "run-1" {
		t.Errorf("active_run_id = %q, want run-1", ws2.ActiveRunID)
	}
	if ws2.LastSeenAt == ws1.LastSeenAt {
		t.Error("last_seen_at 应该更新")
	}
}

func TestUpdateFromHeartbeatMultipleWorkers(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "w1", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z",
	})
	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "w2", Status: "busy", ActiveRunID: "r1", ReportedAt: "2026-07-07T10:00:01Z",
	})

	if v.Count() != 2 {
		t.Errorf("Count = %d, want 2", v.Count())
	}

	all := v.GetAll()
	if len(all) != 2 {
		t.Errorf("GetAll len = %d, want 2", len(all))
	}
}

func TestUpdateFromHeartbeatEmptyWorkerID(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z",
	})
	if v.Count() != 0 {
		t.Errorf("空 worker_id 不应更新: Count = %d", v.Count())
	}
}

func TestStaleDetection(t *testing.T) {
	// 使用很短的超时（50ms），sleep 明显超过它（150ms）
	v := NewWorkerStatusView(50 * time.Millisecond)

	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "worker-01", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z",
	})

	// 刚更新后不应 stale
	ws, _ := v.Get("worker-01")
	if ws.IsStale {
		t.Error("刚更新的 worker 不应为 stale")
	}

	// 等待超过阈值
	time.Sleep(150 * time.Millisecond)

	ws, _ = v.Get("worker-01")
	if !ws.IsStale {
		t.Error("超过阈值的 worker 应为 stale")
	}
}

func TestStaleDetectionAfterRecalculate(t *testing.T) {
	v := NewWorkerStatusView(50 * time.Millisecond)

	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "fast-worker", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z",
	})

	time.Sleep(150 * time.Millisecond)

	// GetAll 内部会调用 RecalculateStale
	all := v.GetAll()
	if len(all) != 1 {
		t.Fatalf("GetAll len = %d", len(all))
	}
	if !all[0].IsStale {
		t.Error("GetAll 应标记 stale worker")
	}
}

func TestGetNonexistentWorker(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	_, ok := v.Get("nonexistent")
	if ok {
		t.Error("不存在的 worker 应返回 false")
	}
}

func TestGetAllReturnsCopies(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "w1", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z",
	})

	all1 := v.GetAll()
	all1[0].Status = "modified"

	all2 := v.GetAll()
	if all2[0].Status == "modified" {
		t.Error("GetAll 返回值应是深拷贝，修改不应影响内部状态")
	}
}

func TestEmptyView(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	if v.Count() != 0 {
		t.Errorf("空 view Count = %d, want 0", v.Count())
	}
	all := v.GetAll()
	if len(all) != 0 {
		t.Errorf("空 view GetAll len = %d, want 0", len(all))
	}
}

func TestStaleTimeoutExact(t *testing.T) {
	// 使用 50ms 超时
	v := NewWorkerStatusView(50 * time.Millisecond)

	v.UpdateFromHeartbeat(runtimeredis.WorkerHeartbeatMessage{
		WorkerID: "w1", Status: "idle", ReportedAt: "2026-07-07T10:00:00Z",
	})

	// 10ms 后: 不应 stale（未超时）
	time.Sleep(10 * time.Millisecond)
	ws, _ := v.Get("w1")
	if ws.IsStale {
		t.Error("未超时不应 stale")
	}

	// 再等 100ms 后: 应 stale（总等待 110ms > 50ms）
	time.Sleep(100 * time.Millisecond)
	ws, _ = v.Get("w1")
	if !ws.IsStale {
		t.Error("超时后应 stale")
	}
}

// -- Phase 6B-1: model status in WorkerStatus --

func TestUpdateFromHeartbeatWithModel(t *testing.T) {
	v := NewWorkerStatusView(DefaultStaleTimeout)
	hb := runtimeredis.WorkerHeartbeatMessage{
		WorkerID:      "worker-model",
		Status:        "idle",
		ReportedAt:    "2026-07-10T10:00:00Z",
		SchemaVersion: "2B-1a.1",
		Model: &runtimeredis.WorkerModelStatus{
			Provider:         "deepseek",
			Protocol:         "openai_chat_completions",
			ModelName:        "deepseek-v4-flash",
			APIKeyConfigured: true,
			ThinkingMode:     "disabled",
			Status:           "configured",
			LastErrorCode:    nil,
		},
	}

	v.UpdateFromHeartbeat(hb)

	ws, ok := v.Get("worker-model")
	if !ok {
		t.Fatal("worker 应存在")
	}
	if ws.Model == nil {
		t.Fatal("WorkerStatus.Model 应为非 nil")
	}
	if ws.Model.Provider != "deepseek" {
		t.Errorf("Provider: got %q, want %q", ws.Model.Provider, "deepseek")
	}
	if ws.Model.Status != "configured" {
		t.Errorf("Status: got %q, want %q", ws.Model.Status, "configured")
	}

	// GetAll 也能返回 model
	all := v.GetAll()
	if len(all) != 1 {
		t.Fatalf("GetAll 应返回 1 个 worker，got %d", len(all))
	}
	if all[0].Model == nil {
		t.Fatal("GetAll 返回的 WorkerStatus.Model 应为非 nil")
	}
}

func TestUpdateFromHeartbeatWithoutModel(t *testing.T) {
	// 旧 heartbeat 无 model 字段
	v := NewWorkerStatusView(DefaultStaleTimeout)
	hb := runtimeredis.WorkerHeartbeatMessage{
		WorkerID:      "worker-old",
		Status:        "idle",
		ReportedAt:    "2026-07-10T10:00:00Z",
		SchemaVersion: "2B-1a.1",
		Model:         nil,
	}

	v.UpdateFromHeartbeat(hb)

	ws, ok := v.Get("worker-old")
	if !ok {
		t.Fatal("worker 应存在")
	}
	if ws.Model != nil {
		t.Errorf("旧 heartbeat 无 model 时 Model 应为 nil，got %+v", ws.Model)
	}
}
