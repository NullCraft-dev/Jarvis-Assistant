package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type ScheduleControlPlane interface {
	ListScheduledTasks(context.Context) (*controlplane.ListScheduledTasksResponse, error)
	CreateScheduledTask(context.Context, controlplane.CreateScheduledTaskRequest) (*controlplane.ScheduledTaskResponse, error)
	UpdateScheduledTask(context.Context, string, controlplane.UpdateScheduledTaskRequest) (*controlplane.ScheduledTaskResponse, error)
	TriggerScheduledTask(context.Context, string) (*controlplane.ScheduledExecutionResponse, error)
}

type ScheduleHandler struct{ controlPlane ScheduleControlPlane }

func NewScheduleHandler(cp ScheduleControlPlane) *ScheduleHandler {
	return &ScheduleHandler{controlPlane: cp}
}

func (h *ScheduleHandler) Collection(w http.ResponseWriter, r *http.Request) {
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "定期任务服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	switch r.Method {
	case http.MethodGet:
		resp, err := h.controlPlane.ListScheduledTasks(ctx)
		if err != nil {
			h.writeError(w, err)
			return
		}
		items := make([]contracts.ScheduledTaskDTO, len(resp.ScheduledTasks))
		for i, item := range resp.ScheduledTasks {
			items[i] = scheduleFromCP(item)
		}
		writeOK(w, contracts.ListScheduledTasksOutput{ScheduledTasks: items})
	case http.MethodPost:
		var input contracts.CreateScheduledTaskInput
		if json.NewDecoder(r.Body).Decode(&input) != nil {
			writeError(w, 400, "VALIDATION_ERROR", "请求格式无效", "validation", false)
			return
		}
		if input.TaskKind == "" {
			input.TaskKind = "knowledge_report"
		}
		if input.SourceMaxResults == 0 {
			input.SourceMaxResults = 5
		}
		resp, err := h.controlPlane.CreateScheduledTask(ctx, controlplane.CreateScheduledTaskRequest{Name: input.Name, UserGoal: input.UserGoal, Recurrence: input.Recurrence, Timezone: input.Timezone, Hour: input.Hour, Minute: input.Minute, Weekday: input.Weekday, WorkspaceID: input.WorkspaceID, TaskKind: input.TaskKind, SourceQuery: input.SourceQuery, SourceMaxResults: input.SourceMaxResults})
		if err != nil {
			h.writeError(w, err)
			return
		}
		writeOK(w, contracts.ScheduledTaskMutationOutput{ScheduledTask: scheduleFromCP(resp.ScheduledTask)})
	default:
		WriteMethodNotAllowed(w, "GET, POST")
	}
}

func (h *ScheduleHandler) Item(w http.ResponseWriter, r *http.Request, id, action string) {
	if _, err := uuid.Parse(id); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "scheduled_task_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "定期任务服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	if action == "trigger" && r.Method == http.MethodPost {
		resp, err := h.controlPlane.TriggerScheduledTask(ctx, id)
		if err != nil {
			h.writeError(w, err)
			return
		}
		writeOK(w, contracts.ScheduledExecutionOutput{Execution: executionFromCP(resp.Execution)})
		return
	}
	if action == "" && r.Method == http.MethodPatch {
		var input contracts.UpdateScheduledTaskInput
		if json.NewDecoder(r.Body).Decode(&input) != nil {
			writeError(w, 400, "VALIDATION_ERROR", "请求格式无效", "validation", false)
			return
		}
		resp, err := h.controlPlane.UpdateScheduledTask(ctx, id, controlplane.UpdateScheduledTaskRequest{ExpectedVersion: input.ExpectedVersion, Status: input.Status})
		if err != nil {
			h.writeError(w, err)
			return
		}
		writeOK(w, contracts.ScheduledTaskMutationOutput{ScheduledTask: scheduleFromCP(resp.ScheduledTask)})
		return
	}
	WriteMethodNotAllowed(w, "PATCH, POST")
}

func (h *ScheduleHandler) writeError(w http.ResponseWriter, err error) {
	if e, ok := err.(*controlplane.ControlPlaneError); ok {
		st, code, msg, cat, rec := mapControlPlaneError(e)
		writeError(w, st, code, msg, cat, rec)
		return
	}
	writeError(w, 503, "SCHEDULE_SERVICE_ERROR", "定期任务服务暂不可用", "storage", true)
}
func scheduleFromCP(v controlplane.ScheduledTaskDTO) contracts.ScheduledTaskDTO {
	return contracts.ScheduledTaskDTO{ID: v.ID, Name: v.Name, UserGoal: v.UserGoal, Recurrence: v.Recurrence, Timezone: v.Timezone, Hour: v.Hour, Minute: v.Minute, Weekday: v.Weekday, WorkspaceID: v.WorkspaceID, Status: v.Status, AuthorizedTools: v.AuthorizedTools, TaskKind: v.TaskKind, SourcePolicy: v.SourcePolicy, NextRunAt: v.NextRunAt, LastRunAt: v.LastRunAt, LastTaskID: v.LastTaskID, LastRunID: v.LastRunID, Version: v.Version, CreatedAt: v.CreatedAt, UpdatedAt: v.UpdatedAt}
}
func executionFromCP(v controlplane.ScheduledExecutionDTO) contracts.ScheduledExecutionDTO {
	return contracts.ScheduledExecutionDTO{ID: v.ID, ScheduledTaskID: v.ScheduledTaskID, ScheduledFor: v.ScheduledFor, Status: v.Status, TaskID: v.TaskID, RunID: v.RunID, Attempts: v.Attempts, ErrorCode: v.ErrorCode, CreatedAt: v.CreatedAt, UpdatedAt: v.UpdatedAt}
}
