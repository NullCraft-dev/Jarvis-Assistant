package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
)

// ── 任务 ──

type CreateTaskRequest struct {
	UserGoal       string `json:"user_goal"`
	WorkspacePath  string `json:"workspace_path,omitempty"`
	WorkspaceID    string `json:"workspace_id,omitempty"`
	ConversationID string `json:"conversation_id,omitempty"`
	Title          string `json:"title,omitempty"`
}

// CreateTaskResponse 权威创建任务响应（与 Python Control Plane shape 一致）。
type CreateTaskResponse struct {
	Task    TaskDTO         `json:"task"`
	Run     RunDTO          `json:"run"`
	Conv    ConversationDTO `json:"conversation"`
	Msg     MessageDTO      `json:"message"`
	Evt     InitialEventDTO `json:"initial_event"`
	TraceID string          `json:"trace_id"`
}

type GetTaskResponse struct {
	Task TaskDTO  `json:"task"`
	Runs []RunDTO `json:"runs"`
}

type TaskDTO struct {
	ID             string `json:"id"`
	ConversationID string `json:"conversation_id"`
	Title          string `json:"title"`
	UserGoal       string `json:"user_goal"`
	Status         string `json:"status"`
	WorkspacePath  string `json:"workspace_path,omitempty"`
	WorkspaceID    string `json:"workspace_id,omitempty"`
	ActiveRunID    string `json:"active_run_id"`
	CreatedAt      string `json:"created_at"`
	UpdatedAt      string `json:"updated_at"`
}

type RunDTO struct {
	ID        string `json:"id"`
	TaskID    string `json:"task_id"`
	AgentID   string `json:"agent_id"`
	Mode      string `json:"mode"`
	Status    string `json:"status"`
	Version   int    `json:"version"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

type ConversationDTO struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at,omitempty"`
}

type MessageDTO struct {
	ID             string `json:"id"`
	Role           string `json:"role"`
	Content        string `json:"content"`
	ConversationID string `json:"conversation_id"`
	TaskID         string `json:"task_id"`
	RunID          string `json:"run_id,omitempty"`
	CreatedAt      string `json:"created_at"`
}

type InitialEventDTO struct {
	ID        string                 `json:"id"`
	EventID   string                 `json:"event_id"`
	Type      string                 `json:"type"`
	RunID     string                 `json:"run_id"`
	Sequence  int64                  `json:"event_sequence"`
	Payload   map[string]interface{} `json:"payload"`
	CreatedAt string                 `json:"created_at"`
}

func (c *Client) CreateTask(ctx context.Context, input CreateTaskRequest) (*CreateTaskResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/tasks", input, &resp); err != nil {
		return nil, err
	}
	var data CreateTaskResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) GetTask(ctx context.Context, taskID string) (*GetTaskResponse, error) {
	var resp apiResponse
	if err := c.get(ctx, "/internal/tasks/"+taskID, &resp); err != nil {
		return nil, err
	}
	var data GetTaskResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析任务投影响应失败: %w", err)
	}
	return &data, nil
}
