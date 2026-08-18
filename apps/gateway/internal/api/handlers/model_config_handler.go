package handlers

import (
	"context"
	"net/http"
	"time"

	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type ModelConfigControlPlane interface {
	GetModelConfig(context.Context) (*controlplane.ModelConfigResponse, error)
	TestModelConnection(context.Context) (*controlplane.ModelTestResponse, error)
}

var _ ModelConfigControlPlane = (*controlplane.Client)(nil)

type WorkerStatusFn func() (status string, lastHeartbeatAt *string, lastErrorCode *string)

// ModelConfigHandler 处理模型配置相关 API（Phase 6）。
type ModelConfigHandler struct {
	controlPlane   ModelConfigControlPlane
	workerStatusFn WorkerStatusFn
}

func NewModelConfigHandler(
	controlPlane ModelConfigControlPlane,
	workerStatusFn WorkerStatusFn,
) *ModelConfigHandler {
	return &ModelConfigHandler{controlPlane: controlPlane, workerStatusFn: workerStatusFn}
}

// GetModelConfig GET /api/model-config
//
// 返回当前模型配置安全投影。Go Gateway 只做代理，不调模型、不访问数据库。
// 合并来自 Python Control Plane 的配置投影和来自 Worker heartbeat 的运行状态。
func (h *ModelConfigHandler) GetModelConfig(w http.ResponseWriter, r *http.Request) {
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "CONTROL_PLANE_UNAVAILABLE", "Control Plane 未连接", "internal", false)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	config, err := h.controlPlane.GetModelConfig(ctx)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, message, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, message, category, recoverable)
			return
		}
		writeError(w, http.StatusBadGateway, "CONTROL_PLANE_ERROR", "获取模型配置失败", "internal", true)
		return
	}

	// 合并 worker heartbeat 中的模型状态
	if h.workerStatusFn != nil {
		workerStatus, lastHB, lastErr := h.workerStatusFn()
		config.WorkerStatus = workerStatus
		config.LastHeartbeatAt = lastHB
		config.LastErrorCode = lastErr
	}

	writeOK(w, config)
}

// TestModelConnection POST /api/model-config/test
//
// 发起模型连通性测试。Go Gateway 只做代理，不调模型、不访问数据库。
func (h *ModelConfigHandler) TestModelConnection(w http.ResponseWriter, r *http.Request) {
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "CONTROL_PLANE_UNAVAILABLE", "Control Plane 未连接", "internal", false)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	result, err := h.controlPlane.TestModelConnection(ctx)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, message, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, message, category, recoverable)
			return
		}
		writeError(w, http.StatusBadGateway, "CONTROL_PLANE_ERROR", "模型测试请求失败", "internal", true)
		return
	}

	writeOK(w, result)
}
