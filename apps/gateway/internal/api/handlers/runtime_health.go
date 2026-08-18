package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
	"github.com/jarvis-assistant/gateway/internal/orchestrator"
	"github.com/jarvis-assistant/gateway/internal/redis"
)

type RuntimeHealthProvider interface {
	GetRuntimeHealth(ctx context.Context) orchestrator.RuntimeHealth
}

type RuntimeDeadLetterProvider interface {
	ListRuntimeDeadLetters(ctx context.Context, source string, limit int, before, errorCode, taskID, runID string) (redis.DeadLetterPage, error)
}

type RuntimeDeadLetterRecordProvider interface {
	GetRuntimeDeadLetter(ctx context.Context, source, id string) (*redis.DeadLetterRecord, error)
}

type RuntimeHealthControlPlane interface {
	GetStorageReconciliation(context.Context, int) (*controlplane.StorageReconciliationResponse, error)
	InspectTerminalEventRepair(context.Context, string) (*controlplane.TerminalEventRepairInspectionResponse, error)
	CreateTerminalEventRepairRequest(context.Context, string) (*controlplane.TerminalEventRepairRequestResponse, error)
	ResolveTerminalEventRepairRequest(context.Context, string, string, string) (*controlplane.TerminalEventRepairResolutionResponse, error)
	InspectDlqRetry(context.Context, controlplane.DlqRetryEvidenceRequest) (*controlplane.DlqRetryInspectionResponse, error)
	CreateDlqRetryRequest(context.Context, controlplane.DlqRetryEvidenceRequest) (*controlplane.DlqRetryRequestResponse, error)
	ResolveDlqRetryRequest(context.Context, string, string, string) (*controlplane.DlqRetryResolutionResponse, error)
}

var _ RuntimeHealthControlPlane = (*controlplane.Client)(nil)

type RuntimeHealthHandler struct {
	provider                 RuntimeHealthProvider
	deadLetterProvider       RuntimeDeadLetterProvider
	deadLetterRecordProvider RuntimeDeadLetterRecordProvider
	controlPlane             RuntimeHealthControlPlane
}

func NewRuntimeHealthHandler(
	provider RuntimeHealthProvider,
	controlPlane RuntimeHealthControlPlane,
) *RuntimeHealthHandler {
	handler := &RuntimeHealthHandler{provider: provider, controlPlane: controlPlane}
	if deadLetterProvider, ok := provider.(RuntimeDeadLetterProvider); ok {
		handler.deadLetterProvider = deadLetterProvider
	}
	if recordProvider, ok := provider.(RuntimeDeadLetterRecordProvider); ok {
		handler.deadLetterRecordProvider = recordProvider
	}
	return handler
}

func (h *RuntimeHealthHandler) GetRuntimeHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, http.MethodGet)
		return
	}
	if h.provider == nil {
		writeOK(w, orchestrator.RuntimeHealth{
			Status: "unavailable", RuntimeBus: "inmemory", GeneratedAt: time.Now().UTC().Format(time.RFC3339Nano),
			Streams: []redis.StreamDiagnostics{}, DeadLetters: []redis.DeadLetterDiagnostics{},
			Warnings: []string{"当前运行时未启用 Redis 诊断"},
		})
		return
	}
	writeOK(w, h.provider.GetRuntimeHealth(r.Context()))
}

// GetStorageReconciliation GET /api/runtime/storage-reconciliation，只读代理 PostgreSQL 真源对账结果。
func (h *RuntimeHealthHandler) GetStorageReconciliation(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, http.MethodGet)
		return
	}
	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > 100 {
			writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "limit 必须在 1-100", "validation", true)
			return
		}
		limit = parsed
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "STORAGE_RECONCILIATION_UNAVAILABLE", "业务真源对账服务不可用", "runtime", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	result, err := h.controlPlane.GetStorageReconciliation(ctx, limit)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, message, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, message, category, recoverable)
			return
		}
		writeError(w, http.StatusServiceUnavailable, "STORAGE_RECONCILIATION_FAILED", "业务真源对账暂不可用", "runtime", true)
		return
	}
	writeOK(w, result)
}

type terminalEventRepairInput struct {
	RunID string `json:"run_id"`
}

func (h *RuntimeHealthHandler) InspectTerminalEventRepair(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, http.MethodPost)
		return
	}
	runID, ok := h.readTerminalEventRepairInput(w, r)
	if !ok {
		return
	}
	result, err := h.controlPlane.InspectTerminalEventRepair(r.Context(), runID)
	if err != nil {
		h.writeTerminalEventRepairError(w, err)
		return
	}
	writeOK(w, result)
}

func (h *RuntimeHealthHandler) CreateTerminalEventRepairRequest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, http.MethodPost)
		return
	}
	runID, ok := h.readTerminalEventRepairInput(w, r)
	if !ok {
		return
	}
	result, err := h.controlPlane.CreateTerminalEventRepairRequest(r.Context(), runID)
	if err != nil {
		h.writeTerminalEventRepairError(w, err)
		return
	}
	writeOK(w, result)
}

func (h *RuntimeHealthHandler) ResolveTerminalEventRepairRequest(
	w http.ResponseWriter, r *http.Request, requestID string,
) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, http.MethodPost)
		return
	}
	if _, err := uuid.Parse(requestID); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "request_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "RUNTIME_REPAIR_UNAVAILABLE", "受控修复服务不可用", "runtime", true)
		return
	}
	var input dlqRetryDecisionInput
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "请求格式无效", "validation", false)
		return
	}
	if input.Decision != "allow_once" && input.Decision != "deny" {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "decision 只允许 allow_once 或 deny", "validation", false)
		return
	}
	if len([]rune(input.Note)) > 500 {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "note 最多 500 字", "validation", false)
		return
	}
	result, err := h.controlPlane.ResolveTerminalEventRepairRequest(
		r.Context(), requestID, input.Decision, input.Note,
	)
	if err != nil {
		h.writeTerminalEventRepairError(w, err)
		return
	}
	writeOK(w, result)
}

func (h *RuntimeHealthHandler) readTerminalEventRepairInput(
	w http.ResponseWriter, r *http.Request,
) (string, bool) {
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "RUNTIME_REPAIR_UNAVAILABLE", "受控修复服务不可用", "runtime", true)
		return "", false
	}
	var input terminalEventRepairInput
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "请求格式无效", "validation", false)
		return "", false
	}
	if _, err := uuid.Parse(input.RunID); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "run_id 格式无效", "validation", false)
		return "", false
	}
	return input.RunID, true
}

func (h *RuntimeHealthHandler) writeTerminalEventRepairError(w http.ResponseWriter, err error) {
	if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
		status, code, message, category, recoverable := mapControlPlaneError(cpErr)
		writeError(w, status, code, message, category, recoverable)
		return
	}
	writeError(w, http.StatusServiceUnavailable, "RUNTIME_REPAIR_FAILED", "受控修复服务暂不可用", "runtime", true)
}

var deadLetterCursorPattern = regexp.MustCompile(`^[0-9]+-[0-9]+$`)
var deadLetterErrorCodePattern = regexp.MustCompile(`^[A-Z0-9_]{1,80}$`)

// ListDeadLetters GET /api/runtime/dead-letters，仅返回脱敏白名单字段。
func (h *RuntimeHealthHandler) ListDeadLetters(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, http.MethodGet)
		return
	}
	query := r.URL.Query()
	source := query.Get("source")
	if source == "" {
		source = "run_queue"
	}
	if source != "run_queue" && source != "worker_command" && source != "runtime_event" {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "source 必须是 run_queue、worker_command 或 runtime_event", "validation", true)
		return
	}
	limit := 20
	if raw := query.Get("limit"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > 50 {
			writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "limit 必须在 1-50", "validation", true)
			return
		}
		limit = parsed
	}
	before := query.Get("before")
	errorCode := query.Get("error_code")
	taskID := query.Get("task_id")
	runID := query.Get("run_id")
	if before != "" && !deadLetterCursorPattern.MatchString(before) {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "before 游标格式无效", "validation", true)
		return
	}
	if errorCode != "" && !deadLetterErrorCodePattern.MatchString(errorCode) {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "error_code 格式无效", "validation", true)
		return
	}
	for _, id := range []string{taskID, runID} {
		if id != "" {
			if _, err := uuid.Parse(id); err != nil {
				writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "task_id 或 run_id 格式无效", "validation", true)
				return
			}
		}
	}
	if h.deadLetterProvider == nil {
		writeError(w, http.StatusServiceUnavailable, "RUNTIME_DIAGNOSTICS_UNAVAILABLE", "DLQ 诊断服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	page, err := h.deadLetterProvider.ListRuntimeDeadLetters(ctx, source, limit, before, errorCode, taskID, runID)
	if err != nil {
		writeError(w, http.StatusBadGateway, "RUNTIME_DIAGNOSTICS_ERROR", "读取 DLQ 诊断记录失败", "internal", true)
		return
	}
	writeOK(w, page)
}

type dlqRetryRecordInput struct {
	Source   string `json:"source"`
	RecordID string `json:"record_id"`
}

type dlqRetryDecisionInput struct {
	Decision string `json:"decision"`
	Note     string `json:"note"`
}

func (h *RuntimeHealthHandler) InspectDeadLetterRetry(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, http.MethodPost)
		return
	}
	record, ok := h.loadDeadLetterForRetry(w, r)
	if !ok {
		return
	}
	response, err := h.controlPlane.InspectDlqRetry(r.Context(), mapDeadLetterEvidence(record))
	if err != nil {
		h.writeDlqRetryError(w, err)
		return
	}
	writeOK(w, response)
}

func (h *RuntimeHealthHandler) CreateDeadLetterRetryRequest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, http.MethodPost)
		return
	}
	record, ok := h.loadDeadLetterForRetry(w, r)
	if !ok {
		return
	}
	response, err := h.controlPlane.CreateDlqRetryRequest(r.Context(), mapDeadLetterEvidence(record))
	if err != nil {
		h.writeDlqRetryError(w, err)
		return
	}
	writeOK(w, response)
}

func (h *RuntimeHealthHandler) ResolveDeadLetterRetryRequest(
	w http.ResponseWriter, r *http.Request, requestID string,
) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, http.MethodPost)
		return
	}
	if _, err := uuid.Parse(requestID); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "request_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "RUNTIME_RECOVERY_UNAVAILABLE", "受控重试服务不可用", "runtime", true)
		return
	}
	var input dlqRetryDecisionInput
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "请求格式无效", "validation", false)
		return
	}
	if input.Decision != "allow_once" && input.Decision != "deny" {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "decision 只允许 allow_once 或 deny", "validation", false)
		return
	}
	if len([]rune(input.Note)) > 500 {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "note 最多 500 字", "validation", false)
		return
	}
	response, err := h.controlPlane.ResolveDlqRetryRequest(r.Context(), requestID, input.Decision, input.Note)
	if err != nil {
		h.writeDlqRetryError(w, err)
		return
	}
	writeOK(w, response)
}

func (h *RuntimeHealthHandler) loadDeadLetterForRetry(
	w http.ResponseWriter, r *http.Request,
) (*redis.DeadLetterRecord, bool) {
	if h.controlPlane == nil || h.deadLetterRecordProvider == nil {
		writeError(w, http.StatusServiceUnavailable, "RUNTIME_RECOVERY_UNAVAILABLE", "受控重试服务不可用", "runtime", true)
		return nil, false
	}
	var input dlqRetryRecordInput
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&input); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "请求格式无效", "validation", false)
		return nil, false
	}
	if input.Source != "run_queue" && input.Source != "worker_command" && input.Source != "runtime_event" {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "source 格式无效", "validation", false)
		return nil, false
	}
	if !deadLetterCursorPattern.MatchString(input.RecordID) {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "record_id 格式无效", "validation", false)
		return nil, false
	}
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	record, err := h.deadLetterRecordProvider.GetRuntimeDeadLetter(ctx, input.Source, input.RecordID)
	if err != nil {
		writeError(w, http.StatusBadGateway, "RUNTIME_DIAGNOSTICS_ERROR", "读取 DLQ 诊断记录失败", "runtime", true)
		return nil, false
	}
	if record == nil {
		writeError(w, http.StatusNotFound, "DLQ_RECORD_NOT_FOUND", "DLQ 诊断记录不存在", "not_found", false)
		return nil, false
	}
	return record, true
}

func mapDeadLetterEvidence(record *redis.DeadLetterRecord) controlplane.DlqRetryEvidenceRequest {
	return controlplane.DlqRetryEvidenceRequest{
		Source: record.Source, RecordID: record.ID,
		OriginalMessageID: record.OriginalMessageID, ErrorCode: record.ErrorCode,
		TaskID: record.TaskID, RunID: record.RunID, PayloadSHA256: record.PayloadSHA256,
	}
}

func (h *RuntimeHealthHandler) writeDlqRetryError(w http.ResponseWriter, err error) {
	if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
		status, code, message, category, recoverable := mapControlPlaneError(cpErr)
		writeError(w, status, code, message, category, recoverable)
		return
	}
	writeError(w, http.StatusServiceUnavailable, "RUNTIME_RECOVERY_FAILED", "受控重试服务暂不可用", "runtime", true)
}
