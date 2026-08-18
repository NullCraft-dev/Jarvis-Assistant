package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
	"github.com/jarvis-assistant/gateway/internal/orchestrator"
)

type RunHandler struct {
	runtime      orchestrator.RuntimeBus
	state        orchestrator.RuntimeStateStore
	controlPlane RunControlPlane
}

type RunControlPlane interface {
	GetRunHistory(context.Context, string) (*controlplane.RunHistoryResponse, error)
	PauseRun(context.Context, string, string) (*controlplane.ControlRunResponse, error)
	ResumeRun(context.Context, string) (*controlplane.ControlRunResponse, error)
	RetryFailedStep(context.Context, string, string) (*controlplane.ControlRunResponse, error)
	CancelRun(context.Context, string, string) (*controlplane.CancelRunResponse, error)
	ResolvePermission(context.Context, controlplane.PermissionDecisionRequest) (*controlplane.PermissionDecisionResponse, error)
	ListPendingPermissions(context.Context, string) (*controlplane.PendingPermissionsResponse, error)
}

var _ RunControlPlane = (*controlplane.Client)(nil)

const (
	realtimeEventPollInterval      = 300 * time.Millisecond
	persistedHistoryPollInterval   = 2 * time.Second
	persistedHistoryRequestTimeout = time.Second
)

func NewRunHandler(
	runtime orchestrator.RuntimeBus,
	state orchestrator.RuntimeStateStore,
	controlPlane RunControlPlane,
) *RunHandler {
	return &RunHandler{runtime: runtime, state: state, controlPlane: controlPlane}
}

// SubscribeEvents GET /api/runs/{id}/events (SSE)
func (h *RunHandler) SubscribeEvents(w http.ResponseWriter, r *http.Request, runID string) {
	historyEvents := []contracts.RuntimeEvent{}
	if h.controlPlane != nil {
		var err error
		historyEvents, err = h.getPersistedRunEvents(r.Context(), runID)
		if err != nil {
			if cpErr, ok := err.(*controlplane.ControlPlaneError); ok && cpErr.Category == "not_found" {
				writeError(w, http.StatusNotFound, "NOT_FOUND", "运行不存在", "not_found", false)
			} else {
				writeError(w, http.StatusServiceUnavailable, "HISTORY_UNAVAILABLE", "运行历史暂不可用", "storage", true)
			}
			return
		}
	} else {
		// in-memory 模式：检查 run 是否存在
		if _, exists := h.state.GetRun(runID); !exists {
			writeError(w, http.StatusNotFound, "NOT_FOUND", "运行不存在", "not_found", false)
			return
		}
	}

	// SSE headers
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "SSE not supported", http.StatusInternalServerError)
		return
	}

	memEvents, _ := h.runtime.GetEvents(runID)
	if memEvents == nil {
		memEvents = []contracts.RuntimeEvent{}
	}

	sent := make(map[string]bool)
	snapshot := mergeRuntimeEvents(historyEvents, memEvents)

	startIndex := 0
	if lastID := r.Header.Get("Last-Event-ID"); lastID != "" {
		for i, event := range snapshot {
			if event.ID == lastID {
				startIndex = i + 1
				break
			}
		}
	}

	// Phase 1: 初始快照。durable 与低延迟内存事件必须按产生时间合并，
	// 不能固定拼成 PostgreSQL + memory，否则暂停恢复后的旧 delta 会落到终态之后。
	terminalSent := false
	for _, event := range snapshot[:startIndex] {
		sent[event.ID] = true
		if isTerminalRuntimeEvent(event) {
			terminalSent = true
		}
	}
	initialEvents, terminalSent := collectUnseenRuntimeEvents(
		snapshot[startIndex:], sent, terminalSent,
	)
	for _, event := range initialEvents {
		select {
		case <-r.Context().Done():
			return
		default:
		}
		writeSSEEvent(w, event)
		flusher.Flush()
	}
	if terminalSent {
		return
	}

	// Phase 2: 内存投影负责低延迟推送；PostgreSQL 权威历史负责有界补偿。
	// Redis 是 Runtime Bus 而非业务真源，实时投影短暂漏失时不能要求用户刷新页面。
	realtimeTicker := time.NewTicker(realtimeEventPollInterval)
	defer realtimeTicker.Stop()

	var historyTicker *time.Ticker
	var historyTick <-chan time.Time
	if h.controlPlane != nil {
		historyTicker = time.NewTicker(persistedHistoryPollInterval)
		historyTick = historyTicker.C
		defer historyTicker.Stop()
	}

	writeUnseen := func(events []contracts.RuntimeEvent) bool {
		var unseen []contracts.RuntimeEvent
		unseen, terminalSent = collectUnseenRuntimeEvents(events, sent, terminalSent)
		for _, event := range unseen {
			writeSSEEvent(w, event)
			flusher.Flush()
		}
		return terminalSent
	}

	for {
		select {
		case <-r.Context().Done():
			return
		case <-realtimeTicker.C:
			currentEvents, err := h.runtime.GetEvents(runID)
			if err != nil {
				continue
			}
			if writeUnseen(currentEvents) {
				return
			}
		case <-historyTick:
			historyCtx, cancel := context.WithTimeout(
				r.Context(), persistedHistoryRequestTimeout,
			)
			persistedEvents, err := h.getPersistedRunEvents(historyCtx, runID)
			cancel()
			if err != nil {
				// 已建立的 SSE 不因一次补偿读取失败而中断；内存投影仍继续推送。
				continue
			}
			if writeUnseen(persistedEvents) {
				return
			}
		}
	}
}

func (h *RunHandler) getPersistedRunEvents(
	ctx context.Context, runID string,
) ([]contracts.RuntimeEvent, error) {
	cpResp, err := h.controlPlane.GetRunHistory(ctx, runID)
	if err != nil {
		return nil, err
	}
	events := make([]contracts.RuntimeEvent, 0, len(cpResp.Events))
	for _, event := range cpResp.Events {
		events = append(events, contracts.RuntimeEvent{
			ID: event.EventID, Type: event.Type, TaskID: event.TaskID,
			RunID: event.RunID, StepID: event.StepID,
			Sequence: event.Sequence, Timestamp: event.Timestamp, Payload: event.Payload,
		})
	}
	return events, nil
}

// mergeRuntimeEvents 对 PostgreSQL durable 历史与 Gateway 内存投影去重并稳定排序。
// event_sequence 只存在于 durable 事件，不能跨 ephemeral 事件直接比较；两类事件共同使用
// Runtime 产生时间排序，sequence 只作为同时间 durable 事件的次级键。
func mergeRuntimeEvents(groups ...[]contracts.RuntimeEvent) []contracts.RuntimeEvent {
	seen := make(map[contracts.ID]bool)
	merged := make([]contracts.RuntimeEvent, 0)
	for _, events := range groups {
		for _, event := range events {
			if seen[event.ID] {
				continue
			}
			seen[event.ID] = true
			merged = append(merged, event)
		}
	}
	sort.SliceStable(merged, func(i, j int) bool {
		left, leftOK := runtimeEventTime(merged[i])
		right, rightOK := runtimeEventTime(merged[j])
		if leftOK && rightOK && !left.Equal(right) {
			return left.Before(right)
		}
		if leftOK != rightOK {
			return leftOK
		}
		if merged[i].Sequence > 0 && merged[j].Sequence > 0 && merged[i].Sequence != merged[j].Sequence {
			return merged[i].Sequence < merged[j].Sequence
		}
		if isTerminalRuntimeEvent(merged[i]) != isTerminalRuntimeEvent(merged[j]) {
			return !isTerminalRuntimeEvent(merged[i])
		}
		return false
	})
	return merged
}

func runtimeEventTime(event contracts.RuntimeEvent) (time.Time, bool) {
	parsed, err := time.Parse(time.RFC3339Nano, event.Timestamp)
	return parsed, err == nil
}

func collectUnseenRuntimeEvents(
	events []contracts.RuntimeEvent,
	sent map[string]bool,
	terminalSent bool,
) ([]contracts.RuntimeEvent, bool) {
	unseen := make([]contracts.RuntimeEvent, 0, len(events))
	for _, event := range mergeRuntimeEvents(events) {
		if sent[event.ID] {
			continue
		}
		sent[event.ID] = true
		// 终态是 Run SSE 的业务边界。迟到的低延迟事件可以留在诊断总线，
		// 但不能在客户端已经观察到终态后再次打开正文或 Timeline。
		if terminalSent {
			continue
		}
		unseen = append(unseen, event)
		if isTerminalRuntimeEvent(event) {
			terminalSent = true
		}
	}
	return unseen, terminalSent
}

func isTerminalRuntimeEvent(event contracts.RuntimeEvent) bool {
	switch event.Type {
	case "agent.run.completed", "agent.run.failed", "agent.run.cancelled":
		return true
	default:
		return false
	}
}

func writeSSEEvent(w http.ResponseWriter, event contracts.RuntimeEvent) {
	payload, _ := json.Marshal(event)
	fmt.Fprintf(w, "id: %s\ndata: %s\n\n", event.ID, payload)
}

// PauseRun POST /api/runs/{id}/pause
func (h *RunHandler) PauseRun(w http.ResponseWriter, r *http.Request, runID string) {
	if h.controlPlane != nil {
		cpResp, err := h.controlPlane.PauseRun(r.Context(), runID, "")
		if err != nil {
			if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
				status, code, message, category, recoverable := mapControlPlaneError(cpErr)
				writeError(w, status, code, message, category, recoverable)
				return
			}
			writeError(w, http.StatusServiceUnavailable, "PAUSE_FAILED", "暂停请求失败", "runtime", true)
			return
		}
		writeOK(w, contracts.AgentRunDTO{ID: contracts.ID(cpResp.RunID), Status: publicRunStatus(cpResp.Status)})
		return
	}
	_, ok := h.state.GetRun(runID)
	if !ok {
		writeError(w, http.StatusNotFound, "NOT_FOUND", "运行不存在", "not_found", false)
		return
	}
	h.state.UpdateRunStatus(runID, "paused")
	run, _ := h.state.GetRun(runID)
	writeOK(w, *run)
}

// ResumeRun POST /api/runs/{id}/resume
func (h *RunHandler) ResumeRun(w http.ResponseWriter, r *http.Request, runID string) {
	if h.controlPlane != nil {
		cpResp, err := h.controlPlane.ResumeRun(r.Context(), runID)
		if err != nil {
			if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
				status, code, message, category, recoverable := mapControlPlaneError(cpErr)
				writeError(w, status, code, message, category, recoverable)
				return
			}
			writeError(w, http.StatusServiceUnavailable, "RESUME_FAILED", "恢复请求失败", "runtime", true)
			return
		}
		writeOK(w, contracts.AgentRunDTO{ID: contracts.ID(cpResp.RunID), Status: publicRunStatus(cpResp.Status)})
		return
	}
	_, ok := h.state.GetRun(runID)
	if !ok {
		writeError(w, http.StatusNotFound, "NOT_FOUND", "运行不存在", "not_found", false)
		return
	}
	h.state.UpdateRunStatus(runID, "running")
	run, _ := h.state.GetRun(runID)
	writeOK(w, *run)
}

// RetryFailedStep POST /api/runs/{run_id}/steps/{step_id}/retry
func (h *RunHandler) RetryFailedStep(w http.ResponseWriter, r *http.Request, runID, stepID string) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "方法不允许", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusNotImplemented, "NOT_IMPLEMENTED", "失败步骤重试需要持久化运行时", "runtime", false)
		return
	}
	cpResp, err := h.controlPlane.RetryFailedStep(r.Context(), runID, stepID)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, message, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, message, category, recoverable)
			return
		}
		writeError(w, http.StatusServiceUnavailable, "STEP_RETRY_FAILED", "失败步骤重试暂不可用", "runtime", true)
		return
	}
	replacement := contracts.AgentRunDTO{
		ID: contracts.ID(cpResp.RunID), TaskID: contracts.ID(cpResp.TaskID),
		AgentID: contracts.ID(cpResp.AgentID), Mode: cpResp.Mode,
		Status:    publicRunStatus(cpResp.Status),
		CreatedAt: cpResp.CreatedAt, UpdatedAt: cpResp.UpdatedAt,
	}
	if projector, ok := h.runtime.(interface {
		SeedAcceptedRun(contracts.TaskDTO, contracts.AgentRunDTO, []contracts.RuntimeEvent)
	}); ok {
		task := contracts.TaskDTO{ID: contracts.ID(cpResp.TaskID), Status: "running", ActiveRunID: replacement.ID}
		if source, exists := h.state.GetRun(runID); exists {
			if existingTask, found := h.state.GetTask(source.TaskID); found {
				task = *existingTask
				task.Status = "running"
				task.ActiveRunID = replacement.ID
			}
		}
		projector.SeedAcceptedRun(task, replacement, []contracts.RuntimeEvent{})
	}
	writeOK(w, replacement)
}

// CancelRun POST /api/runs/{id}/cancel
//
// Python Control Plane 模式：只走 Control Plane → Outbox → Redis。
// Go 不直接发布 Redis cancel command——避免双发布。
func (h *RunHandler) CancelRun(w http.ResponseWriter, r *http.Request, runID string) {
	if h.controlPlane != nil {
		cpResp, err := h.controlPlane.CancelRun(r.Context(), runID, "")
		if err != nil {
			if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
				status, code, message, category, recoverable := mapControlPlaneError(cpErr)
				writeError(w, status, code, message, category, recoverable)
				return
			}
			writeError(w, http.StatusServiceUnavailable, "CANCEL_FAILED", "取消失败", "runtime", true)
			return
		}
		writeOK(w, contracts.AgentRunDTO{
			ID: contracts.ID(cpResp.RunID), Status: publicRunStatus(cpResp.Status),
		})
		return
	}

	run, err := h.runtime.CancelRun(runID)
	if err != nil {
		writeError(w, http.StatusNotFound, "NOT_FOUND", "运行不存在", "not_found", false)
		return
	}
	writeOK(w, *run)
}

// ResolvePermission POST /api/permissions/resolve
//
// Python Control Plane 模式：只走 Control Plane → Outbox → Redis。
// Go 不直接发布 Redis permission decision。
func (h *RunHandler) ResolvePermission(w http.ResponseWriter, r *http.Request) {
	var decision contracts.PermissionDecisionDTO
	if err := json.NewDecoder(r.Body).Decode(&decision); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "请求格式无效", "validation", true)
		return
	}

	if h.controlPlane != nil {
		cpResp, err := h.controlPlane.ResolvePermission(r.Context(), controlplane.PermissionDecisionRequest{
			RequestID: decision.RequestID, Decision: decision.Decision, Note: decision.Note,
		})
		if err != nil {
			if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
				status, code, message, category, recoverable := mapControlPlaneError(cpErr)
				writeError(w, status, code, message, category, recoverable)
				return
			}
			writeError(w, http.StatusServiceUnavailable, "PERMISSION_FAILED", "权限决策失败", "permission", true)
			return
		}
		scope := contracts.PermissionScopeDTO{
			Type:          stringValue(cpResp.Request.Scope["type"]),
			WorkspacePath: stringValue(cpResp.Request.Scope["workspace_path"]),
			Path:          stringValue(cpResp.Request.Scope["path"]),
			ToolName:      stringValue(cpResp.Request.Scope["tool_name"]),
		}
		writeOK(w, contracts.ResolvePermissionOutput{
			Request: contracts.PermissionRequestDTO{
				ID: cpResp.Request.ID, TaskID: cpResp.Request.TaskID,
				RunID: cpResp.Request.RunID, StepID: cpResp.Request.StepID,
				ToolName:      cpResp.Request.ToolName,
				ActionSummary: cpResp.Request.ActionSummary,
				Reason:        cpResp.Request.Reason, RiskLevel: cpResp.Request.RiskLevel,
				Scope: scope, ArgumentsSummary: cpResp.Request.ArgumentsSummary,
				AllowedDecisions: cpResp.Request.AllowedDecisions,
				CreatedAt:        cpResp.Request.CreatedAt,
				ExpiresAt:        cpResp.Request.ExpiresAt,
				Status:           cpResp.Request.Status, Decision: cpResp.Request.Decision,
			},
			Events: permissionDecisionResponseEvents(cpResp, decision.Decision),
		})
		return
	}

	permReq, events, err := h.runtime.ResolvePermission(decision)
	if err != nil {
		writeError(w, http.StatusNotFound, "NOT_FOUND", "权限请求不存在", "not_found", false)
		return
	}
	writeOK(w, map[string]interface{}{"request": *permReq, "events": events})
}

// permissionDecisionResponseEvents 把 Control Plane 已持久化接受的权限决定
// 立即投影成响应事件。Worker 仍是工具结果和最终 durable permission.resolved
// 的 owner；这个 acknowledgement 只表示“等待授权已经结束”，不能表示工具完成。
//
// Permission resume 可能包含 rag.await_ingestion 等长等待。若只等待 Worker 在整段
// resume 收口后批量发布事件，当前页面会在此期间错误地继续显示“等待授权”。
func permissionDecisionResponseEvents(
	cpResp *controlplane.PermissionDecisionResponse,
	decision string,
) []contracts.RuntimeEvent {
	if len(cpResp.Events) > 0 {
		events := make([]contracts.RuntimeEvent, 0, len(cpResp.Events))
		for _, event := range cpResp.Events {
			events = append(events, contracts.RuntimeEvent{
				ID: event.EventID, Type: event.Type,
				TaskID: event.TaskID, RunID: event.RunID, StepID: event.StepID,
				Timestamp: event.Timestamp, Payload: event.Payload,
			})
		}
		return events
	}

	request := cpResp.Request
	eventID := uuid.NewSHA1(
		uuid.NameSpaceURL,
		[]byte("jarvis:permission-decision-ack:"+request.ID+":"+decision),
	).String()
	return []contracts.RuntimeEvent{{
		ID: eventID, Type: "permission.resolved",
		TaskID: request.TaskID, RunID: request.RunID, StepID: request.StepID,
		Timestamp: time.Now().UTC().Format(time.RFC3339Nano),
		Payload: map[string]interface{}{
			"request_id":   request.ID,
			"decision":     decision,
			"acknowledged": true,
		},
	}}
}

func (h *RunHandler) ListPendingPermissions(w http.ResponseWriter, r *http.Request, runID string) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "METHOD_NOT_ALLOWED", "方法不允许", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeOK(w, map[string]interface{}{"requests": []contracts.PermissionRequestDTO{}})
		return
	}
	resp, err := h.controlPlane.ListPendingPermissions(r.Context(), runID)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, message, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, message, category, recoverable)
			return
		}
		writeError(w, http.StatusServiceUnavailable, "PERMISSION_LIST_FAILED", "读取权限请求失败", "storage", true)
		return
	}
	requests := make([]contracts.PermissionRequestDTO, 0, len(resp.Requests))
	for _, item := range resp.Requests {
		requests = append(requests, contracts.PermissionRequestDTO{
			ID: item.ID, TaskID: item.TaskID, RunID: item.RunID, StepID: item.StepID,
			ToolName: item.ToolName, ActionSummary: item.ActionSummary,
			Reason: item.Reason, RiskLevel: item.RiskLevel,
			Scope: contracts.PermissionScopeDTO{
				Type: stringValue(item.Scope["type"]), WorkspacePath: stringValue(item.Scope["workspace_path"]),
				Path: stringValue(item.Scope["path"]), ToolName: stringValue(item.Scope["tool_name"]),
			},
			ArgumentsSummary: item.ArgumentsSummary, AllowedDecisions: item.AllowedDecisions,
			CreatedAt: item.CreatedAt, ExpiresAt: item.ExpiresAt,
			Status: item.Status, Decision: item.Decision,
		})
	}
	writeOK(w, map[string]interface{}{"requests": requests})
}

func stringValue(value interface{}) string {
	if text, ok := value.(string); ok {
		return text
	}
	return ""
}

func ExtractIDFromPath(path, prefix string) string {
	trimmed := strings.TrimPrefix(path, prefix)
	trimmed = strings.TrimPrefix(trimmed, "/")
	if idx := strings.Index(trimmed, "/"); idx >= 0 {
		trimmed = trimmed[:idx]
	}
	return trimmed
}
