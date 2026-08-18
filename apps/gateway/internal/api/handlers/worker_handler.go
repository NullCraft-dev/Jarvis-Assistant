// WorkerHandler 处理 worker status 相关 API（3B heartbeat）。
//
// 职责：
//   - GET /api/runtime/workers：返回 WorkerStatus 列表
//
// 不负责：
//   - 成为 Worker 状态业务真源
//   - 管理 worker 生命周期
//   - 直接访问 Redis
package handlers

import (
	"net/http"

	"github.com/jarvis-assistant/gateway/internal/orchestrator"
)

// WorkerStatusProvider 提供 worker 状态查询。
// RedisRuntimeBus 实现此接口。
type WorkerStatusProvider interface {
	GetWorkerStatuses() []orchestrator.WorkerStatus
}

// WorkerHandler 处理 worker status 相关 API。
type WorkerHandler struct {
	provider WorkerStatusProvider
}

// NewWorkerHandler 创建 WorkerHandler。
// provider 为 nil 时返回空列表（inmemory 模式）。
func NewWorkerHandler(provider WorkerStatusProvider) *WorkerHandler {
	return &WorkerHandler{provider: provider}
}

// GetWorkers GET /api/runtime/workers
//
// 返回所有已知 worker 的状态列表，使用 ApiResult 包装。
// inmemory 模式下 provider 为 nil，返回空列表。
//
// 响应示例：
//
//	{
//	  "ok": true,
//	  "data": {
//	    "workers": [
//	      {
//	        "worker_id": "worker-01",
//	        "status": "idle",
//	        "active_run_id": "",
//	        "reported_at": "2026-07-07T10:00:00Z",
//	        "last_seen_at": "2026-07-07T10:00:01Z",
//	        "is_stale": false
//	      }
//	    ]
//	  }
//	}
func (h *WorkerHandler) GetWorkers(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "仅支持 GET 方法", "validation", false)
		return
	}

	var workers []orchestrator.WorkerStatus
	if h.provider != nil {
		workers = h.provider.GetWorkerStatuses()
	}
	if workers == nil {
		workers = []orchestrator.WorkerStatus{}
	}

	writeOK(w, map[string]interface{}{
		"workers": workers,
	})
}
