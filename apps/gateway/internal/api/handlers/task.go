package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
	"github.com/jarvis-assistant/gateway/internal/orchestrator"
)

type TaskHandler struct {
	runtime      orchestrator.RuntimeBus
	state        orchestrator.RuntimeStateStore
	controlPlane TaskControlPlane
}

type TaskControlPlane interface {
	CreateTask(context.Context, controlplane.CreateTaskRequest) (*controlplane.CreateTaskResponse, error)
	ListTasks(context.Context, int, int) (*controlplane.ListTasksResponse, error)
	GetTaskHistory(context.Context, string) (*controlplane.TaskHistoryResponse, error)
	ListConversations(context.Context, int, int) (*controlplane.ListConversationsResponse, error)
	GetConversation(context.Context, string, int, string) (*controlplane.GetConversationResponse, error)
}

var _ TaskControlPlane = (*controlplane.Client)(nil)

func NewTaskHandler(
	runtime orchestrator.RuntimeBus,
	state orchestrator.RuntimeStateStore,
	controlPlane TaskControlPlane,
) *TaskHandler {
	return &TaskHandler{runtime: runtime, state: state, controlPlane: controlPlane}
}

// CreateTask POST /api/tasks
//
// Python Control Plane 模式：
//
//	Go 校验 DTO → 调用 Python Control Plane（权威持久化）
//	→ Python 返回完整 Task/Run DTO（单一权威 ID）
//	→ Go 用权威 ID 创建 in-memory 投影（只读，不生成新 ID，不入队）
//
// 禁止：Go 在 Control Plane 模式下调用 PrepareRun() 生成第二套 ID 或重复入队。
func (h *TaskHandler) CreateTask(w http.ResponseWriter, r *http.Request) {
	var input contracts.CreateTaskInput
	if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "请求格式无效", "validation", true)
		return
	}
	if input.UserGoal == "" {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "user_goal 不能为空", "validation", true)
		return
	}

	// Python Control Plane 路径：单一权威入口
	if h.controlPlane != nil {
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()

		cpResp, cpErr := h.controlPlane.CreateTask(ctx, controlplane.CreateTaskRequest{
			UserGoal:       input.UserGoal,
			WorkspacePath:  input.WorkspacePath,
			WorkspaceID:    input.WorkspaceID,
			ConversationID: input.ConversationID,
		})
		if cpErr != nil {
			if typed, ok := cpErr.(*controlplane.ControlPlaneError); ok {
				status, code, message, category, recoverable := mapControlPlaneError(typed)
				writeError(w, status, code, message, category, recoverable)
				return
			}
			writeError(w, http.StatusServiceUnavailable, "TASK_CREATE_FAILED",
				"创建任务失败", "storage", true)
			return
		}

		// 使用权威 ID 创建只读实时投影（不生成新 ID，不入队 Redis）
		h.seedProjectionFromAuthority(cpResp)

		writeOK(w, contracts.CreateTaskOutput{
			Task: contracts.TaskDTO{
				ID:             contracts.ID(cpResp.Task.ID),
				ConversationID: contracts.ID(cpResp.Task.ConversationID),
				Title:          cpResp.Task.Title,
				UserGoal:       cpResp.Task.UserGoal,
				Status:         contracts.TaskStatus(cpResp.Task.Status),
				ActiveRunID:    contracts.ID(cpResp.Run.ID),
				WorkspacePath:  cpResp.Task.WorkspacePath,
				WorkspaceID:    contracts.ID(cpResp.Task.WorkspaceID),
				CreatedAt:      cpResp.Task.CreatedAt,
				UpdatedAt:      cpResp.Task.UpdatedAt,
			},
			Run: contracts.AgentRunDTO{
				ID:        contracts.ID(cpResp.Run.ID),
				TaskID:    contracts.ID(cpResp.Run.TaskID),
				AgentID:   cpResp.Run.AgentID,
				Mode:      cpResp.Run.Mode,
				Status:    publicRunStatus(cpResp.Run.Status),
				CreatedAt: cpResp.Run.CreatedAt,
				UpdatedAt: cpResp.Run.UpdatedAt,
			},
			Conversation: contracts.ConversationDTO{
				ID: contracts.ID(cpResp.Conv.ID), Title: cpResp.Conv.Title,
				CreatedAt: cpResp.Conv.CreatedAt, UpdatedAt: cpResp.Conv.UpdatedAt,
			},
			Message: contracts.MessageDTO{
				ID: contracts.ID(cpResp.Msg.ID), ConversationID: contracts.ID(cpResp.Msg.ConversationID),
				TaskID: contracts.ID(cpResp.Msg.TaskID), Role: cpResp.Msg.Role,
				Content: cpResp.Msg.Content, CreatedAt: cpResp.Msg.CreatedAt,
			},
		})
		return
	}

	// In-memory fallback（仅测试/开发，无 Control Plane）
	task, run, _, err := h.runtime.PrepareRun(input)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "RUNTIME_ERROR", "创建运行失败", "runtime", false)
		return
	}
	writeOK(w, contracts.CreateTaskOutput{Task: *task, Run: *run})
}

// seedProjectionFromAuthority 使用 Control Plane 返回的权威 ID 初始化 in-memory 投影。
// 不生成新 ID，不入队 Redis，不修改 PostgreSQL。
func (h *TaskHandler) seedProjectionFromAuthority(resp *controlplane.CreateTaskResponse) {
	// 尝试通过 RuntimeBus 写入只读投影
	if projector, ok := h.runtime.(interface {
		SeedAcceptedRun(contracts.TaskDTO, contracts.AgentRunDTO, []contracts.RuntimeEvent)
	}); ok {
		task := contracts.TaskDTO{
			ID:             contracts.ID(resp.Task.ID),
			ConversationID: contracts.ID(resp.Task.ConversationID),
			Title:          resp.Task.Title,
			UserGoal:       resp.Task.UserGoal,
			Status:         contracts.TaskStatus(resp.Task.Status),
			ActiveRunID:    contracts.ID(resp.Run.ID),
			WorkspacePath:  resp.Task.WorkspacePath,
			WorkspaceID:    contracts.ID(resp.Task.WorkspaceID),
			CreatedAt:      resp.Task.CreatedAt,
			UpdatedAt:      resp.Task.UpdatedAt,
		}
		run := contracts.AgentRunDTO{
			ID:        contracts.ID(resp.Run.ID),
			TaskID:    contracts.ID(resp.Run.TaskID),
			AgentID:   resp.Run.AgentID,
			Mode:      resp.Run.Mode,
			Status:    publicRunStatus(resp.Run.Status),
			CreatedAt: resp.Run.CreatedAt,
			UpdatedAt: resp.Run.UpdatedAt,
		}
		initialEvents := []contracts.RuntimeEvent{
			{
				ID:        resp.Evt.EventID,
				Type:      resp.Evt.Type,
				RunID:     resp.Evt.RunID,
				TaskID:    resp.Task.ID,
				Timestamp: resp.Evt.CreatedAt,
				Payload:   convertPayload(resp.Evt.Payload),
			},
		}
		projector.SeedAcceptedRun(task, run, initialEvents)
	}
}

func convertPayload(p map[string]interface{}) map[string]interface{} {
	if p == nil {
		return map[string]interface{}{}
	}
	return p
}

// ListTasks GET /api/tasks
func (h *TaskHandler) ListTasks(w http.ResponseWriter, r *http.Request) {
	if h.controlPlane != nil {
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		cpResp, err := h.controlPlane.ListTasks(ctx, 50, 0)
		if err != nil {
			writeError(w, http.StatusServiceUnavailable, "STORAGE_ERROR", "读取任务列表失败", "storage", false)
			return
		}
		tasks := make([]contracts.TaskDTO, len(cpResp.Tasks))
		for i, t := range cpResp.Tasks {
			tasks[i] = contracts.TaskDTO{
				ID: contracts.ID(t.ID), ConversationID: contracts.ID(t.ConversationID),
				Title: t.Title, UserGoal: t.UserGoal,
				Status: contracts.TaskStatus(t.Status), ActiveRunID: contracts.ID(t.ActiveRunID),
				WorkspacePath: t.WorkspacePath,
				WorkspaceID:   contracts.ID(t.WorkspaceID),
				CreatedAt:     t.CreatedAt, UpdatedAt: t.UpdatedAt,
			}
		}
		writeOK(w, contracts.ListTasksOutput{Tasks: tasks})
		return
	}
	tasks := h.state.ListTasks()
	writeOK(w, contracts.ListTasksOutput{Tasks: tasks})
}

// GetTask GET /api/tasks/{id}
func (h *TaskHandler) GetTask(w http.ResponseWriter, r *http.Request, taskID string) {
	if h.controlPlane != nil {
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		cpResp, err := h.controlPlane.GetTaskHistory(ctx, taskID)
		if err != nil {
			if cpErr, ok := err.(*controlplane.ControlPlaneError); ok && cpErr.Category == "not_found" {
				writeError(w, http.StatusNotFound, "NOT_FOUND", "任务不存在", "not_found", false)
				return
			}
			writeError(w, http.StatusServiceUnavailable, "STORAGE_ERROR", "读取任务失败", "storage", false)
			return
		}
		task := contracts.TaskDTO{
			ID: contracts.ID(cpResp.Task.ID), ConversationID: contracts.ID(cpResp.Task.ConversationID),
			Title: cpResp.Task.Title, UserGoal: cpResp.Task.UserGoal,
			Status:        contracts.TaskStatus(cpResp.Task.Status),
			WorkspacePath: cpResp.Task.WorkspacePath,
			WorkspaceID:   contracts.ID(cpResp.Task.WorkspaceID),
			CreatedAt:     cpResp.Task.CreatedAt, UpdatedAt: cpResp.Task.UpdatedAt,
		}
		var activeRun *contracts.AgentRunDTO
		if len(cpResp.Runs) > 0 {
			r := cpResp.Runs[0]
			activeRun = &contracts.AgentRunDTO{
				ID: contracts.ID(r.ID), TaskID: contracts.ID(taskID),
				Status: publicRunStatus(r.Status), AgentID: "default",
			}
			task.ActiveRunID = activeRun.ID
		}
		writeOK(w, contracts.TaskDetailOutput{Task: task, ActiveRun: activeRun, Steps: []contracts.ExecutionStepDTO{}, Artifacts: []contracts.ArtifactDTO{}})
		return
	}

	task, ok := h.state.GetTask(taskID)
	if !ok {
		writeError(w, http.StatusNotFound, "NOT_FOUND", "任务不存在", "not_found", false)
		return
	}
	var activeRun *contracts.AgentRunDTO
	if task.ActiveRunID != "" {
		if run, ok := h.state.GetRun(task.ActiveRunID); ok {
			activeRun = run
		}
	}
	writeOK(w, contracts.TaskDetailOutput{Task: *task, ActiveRun: activeRun, Steps: []contracts.ExecutionStepDTO{}, Artifacts: []contracts.ArtifactDTO{}})
}

// ListConversations GET /api/conversations
func (h *TaskHandler) ListConversations(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeMethodNotAllowed(w)
		return
	}
	if h.controlPlane == nil {
		writeOK(w, contracts.ListConversationsOutput{Conversations: []contracts.ConversationDTO{}})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	cpResp, err := h.controlPlane.ListConversations(ctx, 50, 0)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "STORAGE_ERROR", "读取会话列表失败", "storage", false)
		return
	}
	convs := make([]contracts.ConversationDTO, len(cpResp.Conversations))
	for i, c := range cpResp.Conversations {
		convs[i] = contracts.ConversationDTO{
			ID: contracts.ID(c.ID), Title: c.Title,
			CreatedAt: c.CreatedAt, UpdatedAt: c.UpdatedAt,
		}
	}
	writeOK(w, contracts.ListConversationsOutput{Conversations: convs})
}

// GetConversation GET /api/conversations/{id}?limit=50&before=<cursor>
func (h *TaskHandler) GetConversation(w http.ResponseWriter, r *http.Request, convID string) {
	if r.Method != http.MethodGet {
		writeMethodNotAllowed(w)
		return
	}
	if convID == "" {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "缺少 conversation id", "validation", true)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusNotFound, "NOT_FOUND", "会话不存在", "not_found", false)
		return
	}

	// 解析 limit 和 before 参数
	q := r.URL.Query()
	limit := 50
	if rawLimit := q.Get("limit"); rawLimit != "" {
		var err error
		limit, err = strconv.Atoi(rawLimit)
		if err != nil || limit < 1 || limit > 100 {
			writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "limit 必须在 1-100", "validation", true)
			return
		}
	}
	before := q.Get("before")

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	cpResp, err := h.controlPlane.GetConversation(ctx, convID, limit, before)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			st, code, msg, cat, rec := mapControlPlaneError(cpErr)
			writeError(w, st, code, msg, cat, rec)
			return
		}
		writeError(w, http.StatusServiceUnavailable, "STORAGE_ERROR", "读取会话失败", "storage", false)
		return
	}
	msgs := make([]contracts.MessageDTO, len(cpResp.Messages))
	for i, m := range cpResp.Messages {
		msgs[i] = contracts.MessageDTO{
			ID: contracts.ID(m.ID), ConversationID: contracts.ID(m.ConversationID),
			TaskID: contracts.ID(m.TaskID), RunID: contracts.ID(m.RunID),
			Role: m.Role, Content: m.Content, CreatedAt: m.CreatedAt,
		}
	}
	writeOK(w, contracts.ConversationDetailOutput{
		Conversation: contracts.ConversationDTO{
			ID: contracts.ID(cpResp.Conversation.ID), Title: cpResp.Conversation.Title,
			CreatedAt: cpResp.Conversation.CreatedAt, UpdatedAt: cpResp.Conversation.UpdatedAt,
		},
		Messages:   msgs,
		NextCursor: cpResp.NextCursor,
	})
}

func titleFromGoal(goal string) string {
	runes := []rune(goal)
	if len(runes) > 100 {
		return string(runes[:100]) + "..."
	}
	return goal
}
