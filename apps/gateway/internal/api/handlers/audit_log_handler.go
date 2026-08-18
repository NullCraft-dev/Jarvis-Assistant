package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type AuditLogControlPlane interface {
	ListAuditLogs(context.Context, controlplane.ListAuditLogsRequest) (*controlplane.ListAuditLogsResponse, error)
	ExportAuditLogs(context.Context, controlplane.ExportAuditLogsRequest) (*http.Response, error)
	PreviewAuditRetention(context.Context, controlplane.PreviewAuditRetentionRequest) (*controlplane.AuditRetentionPreviewResponse, error)
	CreateAuditRetentionRequest(context.Context, controlplane.CreateAuditRetentionRequest) (*controlplane.CreateAuditRetentionResponse, error)
	ResolveAuditRetentionRequest(context.Context, string, controlplane.ResolveAuditRetentionRequest) (*controlplane.AuditRetentionResolutionResponse, error)
}

var _ AuditLogControlPlane = (*controlplane.Client)(nil)

// AuditLogHandler 代理只读审计查询；Go 不读取数据库、不重新解释审计内容。
type AuditLogHandler struct{ controlPlane AuditLogControlPlane }

func NewAuditLogHandler(controlPlane AuditLogControlPlane) *AuditLogHandler {
	return &AuditLogHandler{controlPlane: controlPlane}
}

// ListAuditLogs GET /api/audit-logs
func (h *AuditLogHandler) ListAuditLogs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, http.MethodGet)
		return
	}
	q := r.URL.Query()
	limit := 50
	if raw := q.Get("limit"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > 100 {
			writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "limit 必须在 1-100", "validation", true)
			return
		}
		limit = parsed
	}
	input := controlplane.ListAuditLogsRequest{Limit: limit, EventType: q.Get("event_type"), Actor: q.Get("actor"), TaskID: q.Get("task_id"), RunID: q.Get("run_id"), Before: q.Get("before")}
	if len(input.EventType) > 50 || len(input.Actor) > 100 || len(input.Before) > 512 {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "审计查询参数无效", "validation", true)
		return
	}
	for _, id := range []string{input.TaskID, input.RunID} {
		if id != "" {
			if _, err := uuid.Parse(id); err != nil {
				writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "task_id 或 run_id 格式无效", "validation", true)
				return
			}
		}
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "CONTROL_PLANE_UNAVAILABLE", "审计查询服务不可用", "internal", true)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	response, err := h.controlPlane.ListAuditLogs(ctx, input)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, msg, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, msg, category, recoverable)
			return
		}
		writeError(w, http.StatusBadGateway, "CONTROL_PLANE_ERROR", "读取审计日志失败", "internal", true)
		return
	}
	logs := make([]contracts.AuditLogDTO, len(response.AuditLogs))
	for i, log := range response.AuditLogs {
		logs[i] = contracts.AuditLogDTO{ID: log.ID, EventType: log.EventType, Actor: log.Actor, ActionSummary: log.ActionSummary, TaskID: log.TaskID, RunID: log.RunID, StepID: log.StepID, ToolCallID: log.ToolCallID, RiskLevel: log.RiskLevel, PermissionDecision: log.PermissionDecision, ResultSummary: log.ResultSummary, ErrorCode: log.ErrorCode, DetailsSummary: log.DetailsSummary, CreatedAt: log.CreatedAt}
	}
	writeOK(w, contracts.ListAuditLogsOutput{AuditLogs: logs, NextCursor: response.NextCursor})
}

// ExportAuditLogs GET /api/audit-logs/export
func (h *AuditLogHandler) ExportAuditLogs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, http.MethodGet)
		return
	}
	q := r.URL.Query()
	format := q.Get("format")
	if format == "" {
		format = "jsonl"
	}
	if format != "jsonl" && format != "csv" {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "format 只支持 jsonl 或 csv", "validation", false)
		return
	}
	maxRows, ok := parseBoundedAuditExportInt(q.Get("max_rows"), 5_000, 1, 10_000)
	if !ok {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "max_rows 必须在 1-10000", "validation", false)
		return
	}
	maxBytes, ok := parseBoundedAuditExportInt(q.Get("max_bytes"), 5*1024*1024, 1_024, 10*1024*1024)
	if !ok {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "max_bytes 必须在 1024-10485760", "validation", false)
		return
	}
	input := controlplane.ExportAuditLogsRequest{
		Format: format, MaxRows: maxRows, MaxBytes: maxBytes,
		EventType: q.Get("event_type"), Actor: q.Get("actor"),
		TaskID: q.Get("task_id"), RunID: q.Get("run_id"), Before: q.Get("before"),
	}
	if len(input.EventType) > 50 || len(input.Actor) > 100 || len(input.Before) > 512 {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "审计导出筛选参数无效", "validation", false)
		return
	}
	for _, id := range []string{input.TaskID, input.RunID} {
		if id != "" {
			if _, err := uuid.Parse(id); err != nil {
				writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "task_id 或 run_id 格式无效", "validation", false)
				return
			}
		}
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "CONTROL_PLANE_UNAVAILABLE", "审计导出服务不可用", "internal", true)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 120*time.Second)
	defer cancel()
	response, err := h.controlPlane.ExportAuditLogs(ctx, input)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, msg, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, msg, category, recoverable)
			return
		}
		writeError(w, http.StatusBadGateway, "CONTROL_PLANE_ERROR", "导出审计日志失败", "internal", true)
		return
	}
	defer response.Body.Close()

	contentType := "application/x-ndjson; charset=utf-8"
	if format == "csv" {
		contentType = "text/csv; charset=utf-8"
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="jarvis-audit-export.%s"`, format))
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("X-Audit-Export-Max-Rows", strconv.Itoa(maxRows))
	w.Header().Set("X-Audit-Export-Max-Bytes", strconv.Itoa(maxBytes))
	w.WriteHeader(http.StatusOK)
	if _, err := io.Copy(w, io.LimitReader(response.Body, int64(maxBytes))); err != nil {
		slog.WarnContext(
			r.Context(),
			"审计导出流转发中断",
			"error_type", fmt.Sprintf("%T", err),
		)
	}
}

// PreviewAuditRetention GET /api/audit-logs/retention/preview
func (h *AuditLogHandler) PreviewAuditRetention(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, http.MethodGet)
		return
	}
	q := r.URL.Query()
	standardDays, ok := parseBoundedAuditExportInt(q.Get("standard_days"), 90, 30, 3_650)
	if !ok {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "standard_days 必须在 30-3650", "validation", false)
		return
	}
	extendedDays, ok := parseBoundedAuditExportInt(q.Get("extended_days"), 365, 30, 3_650)
	if !ok || extendedDays <= standardDays {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "extended_days 必须在 30-3650 且大于 standard_days", "validation", false)
		return
	}
	maxScan, ok := parseBoundedAuditExportInt(q.Get("max_scan"), 1_000, 1, 10_000)
	if !ok {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "max_scan 必须在 1-10000", "validation", false)
		return
	}
	maxCandidates, ok := parseBoundedAuditExportInt(q.Get("max_candidates"), 100, 1, 1_000)
	if !ok {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "max_candidates 必须在 1-1000", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "CONTROL_PLANE_UNAVAILABLE", "审计保留预演服务不可用", "internal", true)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	response, err := h.controlPlane.PreviewAuditRetention(
		ctx,
		controlplane.PreviewAuditRetentionRequest{
			StandardDays: standardDays, ExtendedDays: extendedDays,
			MaxScan: maxScan, MaxCandidates: maxCandidates,
		},
	)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, msg, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, msg, category, recoverable)
			return
		}
		writeError(w, http.StatusBadGateway, "CONTROL_PLANE_ERROR", "预演审计保留策略失败", "internal", true)
		return
	}
	writeOK(w, contracts.AuditRetentionPreviewDTO{
		DryRun: response.DryRun, StandardDays: response.StandardDays,
		ExtendedDays: response.ExtendedDays, StandardBefore: response.StandardBefore,
		ExtendedBefore: response.ExtendedBefore, MaxScan: response.MaxScan,
		MaxCandidates: response.MaxCandidates, ScannedRecords: response.ScannedRecords,
		CandidateRecords: response.CandidateRecords, ProtectedRecords: response.ProtectedRecords,
		ExtendedRetainedRecords: response.ExtendedRetainedRecords, HasMore: response.HasMore,
	})
}

// CreateAuditRetentionRequest POST /api/audit-logs/retention/requests
func (h *AuditLogHandler) CreateAuditRetentionRequest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, http.MethodPost)
		return
	}
	var input contracts.CreateAuditRetentionRequestInput
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4_096)).Decode(&input); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "审计保留确认参数无效", "validation", false)
		return
	}
	if !validAuditRetentionBounds(
		input.StandardDays,
		input.ExtendedDays,
		input.MaxScan,
		input.MaxCandidates,
	) {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "审计保留策略边界无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "CONTROL_PLANE_UNAVAILABLE", "审计保留确认服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	response, err := h.controlPlane.CreateAuditRetentionRequest(
		ctx,
		controlplane.CreateAuditRetentionRequest{
			StandardDays: input.StandardDays, ExtendedDays: input.ExtendedDays,
			MaxScan: input.MaxScan, MaxCandidates: input.MaxCandidates,
		},
	)
	if err != nil {
		h.writeControlPlaneError(w, err, "创建审计保留确认失败")
		return
	}
	writeOK(w, contracts.CreateAuditRetentionRequestOutput{
		Request: auditPermissionDTO(response.Request),
	})
}

// ResolveAuditRetentionRequest POST /api/audit-logs/retention/requests/{id}/resolve
func (h *AuditLogHandler) ResolveAuditRetentionRequest(
	w http.ResponseWriter,
	r *http.Request,
	requestID string,
) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, http.MethodPost)
		return
	}
	if _, err := uuid.Parse(requestID); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "request_id 格式无效", "validation", false)
		return
	}
	var input contracts.ResolveAuditRetentionRequestInput
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2_048)).Decode(&input); err != nil ||
		(input.Decision != "allow_once" && input.Decision != "deny") ||
		len(input.Note) > 500 {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "审计保留决策参数无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "CONTROL_PLANE_UNAVAILABLE", "审计保留执行服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	response, err := h.controlPlane.ResolveAuditRetentionRequest(
		ctx,
		requestID,
		controlplane.ResolveAuditRetentionRequest{
			Decision: input.Decision,
			Note:     input.Note,
		},
	)
	if err != nil {
		h.writeControlPlaneError(w, err, "执行审计保留策略失败")
		return
	}
	writeOK(w, contracts.AuditRetentionResolutionDTO{
		Permission:     auditPermissionDTO(response.Permission),
		DeletedRecords: response.DeletedRecords,
		HasMore:        response.HasMore,
	})
}

func (h *AuditLogHandler) writeControlPlaneError(w http.ResponseWriter, err error, fallback string) {
	if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
		status, code, msg, category, recoverable := mapControlPlaneError(cpErr)
		writeError(w, status, code, msg, category, recoverable)
		return
	}
	writeError(w, http.StatusBadGateway, "CONTROL_PLANE_ERROR", fallback, "internal", true)
}

func auditPermissionDTO(input controlplane.PermissionRequestDTO) contracts.PermissionRequestDTO {
	return contracts.PermissionRequestDTO{
		ID: input.ID, TaskID: input.TaskID, RunID: input.RunID, StepID: input.StepID,
		ToolName: input.ToolName, ActionSummary: input.ActionSummary, Reason: input.Reason,
		RiskLevel: input.RiskLevel,
		Scope: contracts.PermissionScopeDTO{
			Type: fmt.Sprint(input.Scope["type"]), Resource: fmt.Sprint(input.Scope["resource"]),
		},
		ArgumentsSummary: input.ArgumentsSummary, AllowedDecisions: input.AllowedDecisions,
		CreatedAt: input.CreatedAt, ExpiresAt: input.ExpiresAt,
		Status: input.Status, Decision: input.Decision,
	}
}

func validAuditRetentionBounds(standardDays, extendedDays, maxScan, maxCandidates int) bool {
	return standardDays >= 30 && standardDays <= 3_650 &&
		extendedDays >= 30 && extendedDays <= 3_650 &&
		extendedDays > standardDays &&
		maxScan >= 1 && maxScan <= 10_000 &&
		maxCandidates >= 1 && maxCandidates <= 1_000
}

func parseBoundedAuditExportInt(raw string, fallback, minimum, maximum int) (int, bool) {
	if raw == "" {
		return fallback, true
	}
	value, err := strconv.Atoi(raw)
	return value, err == nil && value >= minimum && value <= maximum
}
