// WorkerStatusView 是 worker 状态的内存视图。
//
// 从 Redis heartbeat stream 读取 WorkerHeartbeatMessage 后更新本视图。
// 本视图仅是 Gateway 进程内临时状态缓存，不是业务真源。
// Redis 仍是 runtime bus，Storage 才是持久化真源。
//
// # 职责
//
//   - 按 worker_id 维护最新心跳状态
//   - 计算 is_stale（基于 lastSeen time.Time 与 stale 阈值）
//   - 提供 GetAll / Get 查询方法（返回深拷贝，LastSeenAt 格式为 RFC3339 ISO 字符串）
//
// # 不负责
//
//   - 成为 Worker 状态业务真源
//   - 持久化到 Storage（本切片不做）
//   - 管理 worker 生命周期（本切片只观察）
//   - 执行 Agent loop / LLM / 工具
//
// # 并发安全
//
//	所有方法使用 sync.RWMutex 保护，支持并发读写。
//
// # Stale 判定
//
//	使用固定阈值：staleTimeout = heartbeatInterval * 3，默认 9s（基于 3000ms interval）。
//	可配置为其他值。
//	lastSeen 由 Gateway 在收到心跳时设置（当前 time.Time），非 worker 字段。
//	若 worker 在 staleTimeout 内无心跳更新，is_stale = true。
//	time.Time 内部提供亚秒精度，避免 RFC3339 秒精度导致的测试不稳定。
package orchestrator

import (
	"sync"
	"time"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	runtimeredis "github.com/jarvis-assistant/gateway/internal/redis"
)

// WorkerStatus 是 Gateway 内存中维护的 worker 状态快照。
//
// 与 WorkerHeartbeatMessage 不同：
//   - LastSeenAt 由 Gateway 在收到心跳时设置（本地时钟），不是 worker 上报字段
//   - IsStale 由 Gateway 基于 threshold 计算，不来自 Redis 消息
type WorkerStatus struct {
	// WorkerID 是 worker 唯一标识
	WorkerID contracts.ID `json:"worker_id"`
	// WorkerKind 是 worker 类型：agent / rag
	WorkerKind string `json:"worker_kind"`
	// Status 是 worker 当前状态：starting / idle / busy / draining / stopped / failed
	Status string `json:"status"`
	// ActiveRunID 是 worker 当前正在处理的 run id，idle 时为空
	ActiveRunID contracts.ID `json:"active_run_id"`
	// ReportedAt 是 worker 上报心跳的时间（ISO 8601）
	ReportedAt string `json:"reported_at"`
	// LastSeenAt 是 Gateway 收到此心跳的本地时间（RFC 3339）
	LastSeenAt string `json:"last_seen_at"`
	// IsStale 表示 worker 是否超时未发送心跳
	IsStale bool `json:"is_stale"`
	// Model 是模型配置状态（Phase 6B-1），来自 heartbeat，可能为空
	Model *runtimeredis.WorkerModelStatus `json:"model,omitempty"`
	// RuntimeBus 是 Redis Runtime Bus 进程级累计指标，来自 heartbeat，可能为空
	RuntimeBus *runtimeredis.WorkerRuntimeBusMetrics `json:"runtime_bus,omitempty"`

	// lastSeen 是内部 time.Time，用于亚秒精度 stale 计算
	lastSeen time.Time
}

// WorkerStatusView 是 worker 状态的内存视图。
//
// 使用 sync.RWMutex 保护内部 map，并发安全。
// 所有查询方法返回深拷贝，防止外部修改内部状态。
type WorkerStatusView struct {
	mu           sync.RWMutex
	workers      map[contracts.ID]*WorkerStatus
	staleTimeout time.Duration
}

// NewWorkerStatusView 创建 WorkerStatusView。
//
// staleTimeout 是判定 worker 失联的阈值。
// 若 worker 在 staleTimeout 内无心跳更新，is_stale = true。
// 默认 9s（对应 heartbeat interval 3000ms × 3）。
func NewWorkerStatusView(staleTimeout time.Duration) *WorkerStatusView {
	if staleTimeout <= 0 {
		staleTimeout = 9 * time.Second
	}
	return &WorkerStatusView{
		workers:      make(map[contracts.ID]*WorkerStatus),
		staleTimeout: staleTimeout,
	}
}

// DefaultStaleTimeout 是默认 stale 阈值。
// heartbeat interval 3000ms × 3 = 9s。
const DefaultStaleTimeout = 9 * time.Second

// UpdateFromHeartbeat 根据 WorkerHeartbeatMessage 更新 worker 状态。
//
// 内部流程：
//  1. 构造 WorkerStatus
//  2. lastSeen 设为 time.Now()（亚秒精度）
//  3. LastSeenAt 设为 RFC3339 格式字符串（供 API 输出）
//  4. is_stale 设为 false（刚收到心跳）
//  5. 保存到内部 map
//
// 若 hb 为空或 worker_id 为空，忽略（不更新）。
// 此方法对非法 heartbeat 不报错，只忽略。
func (v *WorkerStatusView) UpdateFromHeartbeat(hb runtimeredis.WorkerHeartbeatMessage) {
	if hb.WorkerID == "" {
		return
	}
	workerKind := hb.WorkerKind
	if workerKind == "" {
		workerKind = "agent"
	}

	now := time.Now()

	v.mu.Lock()
	defer v.mu.Unlock()

	v.workers[hb.WorkerID] = &WorkerStatus{
		WorkerID:    hb.WorkerID,
		WorkerKind:  workerKind,
		Status:      hb.Status,
		ActiveRunID: hb.ActiveRunID,
		ReportedAt:  hb.ReportedAt,
		LastSeenAt:  now.UTC().Format(time.RFC3339Nano),
		IsStale:     false,
		lastSeen:    now,
		Model:       hb.Model,
		RuntimeBus:  hb.RuntimeBus,
	}
}

// RecalculateStale 重新计算所有 worker 的 is_stale 状态。
//
// 调用时机：查询前或定期刷新时。
// 根据 lastSeen (time.Time) 与当前时间的间隔判断。
func (v *WorkerStatusView) RecalculateStale() {
	now := time.Now()

	v.mu.Lock()
	defer v.mu.Unlock()

	for _, ws := range v.workers {
		ws.IsStale = now.Sub(ws.lastSeen) > v.staleTimeout
	}
}

// GetAll 返回所有 worker 状态的深拷贝列表。
//
// 返回前自动调用 RecalculateStale 刷新 stale 状态。
func (v *WorkerStatusView) GetAll() []WorkerStatus {
	v.RecalculateStale()

	v.mu.RLock()
	defer v.mu.RUnlock()

	result := make([]WorkerStatus, 0, len(v.workers))
	for _, ws := range v.workers {
		result = append(result, *ws) // 值拷贝
	}
	return result
}

// Get 返回单个 worker 状态，若不存在返回 false。
//
// 返回 WorkerStatus 的值拷贝。
// 使用内部 lastSeen (time.Time) 计算 is_stale，保证亚秒精度。
func (v *WorkerStatusView) Get(workerID contracts.ID) (WorkerStatus, bool) {
	v.mu.RLock()
	defer v.mu.RUnlock()

	ws, ok := v.workers[workerID]
	if !ok {
		return WorkerStatus{}, false
	}

	cp := *ws
	cp.IsStale = time.Since(ws.lastSeen) > v.staleTimeout
	return cp, true
}

// Count 返回当前已知的 worker 数量。
func (v *WorkerStatusView) Count() int {
	v.mu.RLock()
	defer v.mu.RUnlock()
	return len(v.workers)
}
