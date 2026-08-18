package handlers

import (
	"encoding/json"
	"net/http"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

// writeOK 写入成功响应。
func writeOK(w http.ResponseWriter, data interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(contracts.ApiResult[interface{}]{
		Ok:   true,
		Data: &data,
	})
}

// writeError 写入错误响应。
func writeError(w http.ResponseWriter, status int, code, message, category string, recoverable bool) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(contracts.ApiResult[interface{}]{
		Ok: false,
		Error: &contracts.AppError{
			Code:        code,
			Message:     message,
			Category:    category,
			Recoverable: recoverable,
		},
	})
}

// WriteAppError 是 Gateway 路由层写入统一 ApiResult/AppError 的公开入口。
// 路由只能选择安全的 HTTP/契约错误，不能直接写入 text/plain 的 http.Error 响应。
func WriteAppError(w http.ResponseWriter, status int, code, message, category string, recoverable bool) {
	writeError(w, status, code, message, category, recoverable)
}

// WriteMethodNotAllowed 写入 405 响应（统一 ApiResult/AppError）。
// allow 是允许的 HTTP method（如 GET、POST）。
func WriteMethodNotAllowed(w http.ResponseWriter, allow string) {
	w.Header().Set("Allow", allow)
	writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "方法不允许", "validation", true)
}

// writeMethodNotAllowed 写入 405 响应（统一 ApiResult/AppError），默认 Allow: GET。
func writeMethodNotAllowed(w http.ResponseWriter) {
	WriteMethodNotAllowed(w, http.MethodGet)
}

// mapControlPlaneError 将 ControlPlaneError 安全映射为 HTTP 状态 + AppError。
//
// 规则：
//
//	validation → 400
//	not_found  → 404
//	permission → 403
//	storage    → 503
//	其他       → 500
//
// 始终保留 code/message/category/recoverable，不透传原始异常或敏感信息。
func mapControlPlaneError(cpErr *controlplane.ControlPlaneError) (int, string, string, string, bool) {
	code := cpErr.Code
	msg := cpErr.Message
	cat := cpErr.Category
	rec := cpErr.Recoverable
	if code == "PERMISSION_CONFLICT" || code == "PERMISSION_NOT_PENDING" || code == "RUN_VERSION_CONFLICT" || code == "RUN_ALREADY_TERMINAL" {
		return http.StatusConflict, code, msg, cat, rec
	}

	switch cat {
	case "validation":
		return http.StatusBadRequest, code, msg, cat, rec
	case "not_found":
		return http.StatusNotFound, code, msg, cat, rec
	case "permission":
		return http.StatusForbidden, code, msg, cat, rec
	case "storage":
		return http.StatusServiceUnavailable, code, msg, cat, rec
	default:
		return http.StatusInternalServerError, code, msg, cat, rec
	}
}

func publicRunStatus(status string) contracts.AgentRunStatus {
	if status == "waiting_permission" {
		return "waiting_for_permission"
	}
	return contracts.AgentRunStatus(status)
}
