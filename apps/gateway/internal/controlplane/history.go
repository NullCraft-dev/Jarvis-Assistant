package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
	observability "github.com/jarvis-assistant/gateway/internal/observability"
	"net/http"
)

// ── 查询 ──

type ListTasksResponse struct {
	Tasks []TaskItem `json:"tasks"`
}

type TaskItem struct {
	ID             string `json:"id"`
	ConversationID string `json:"conversation_id"`
	Title          string `json:"title"`
	UserGoal       string `json:"user_goal"`
	Status         string `json:"status"`
	WorkspacePath  string `json:"workspace_path,omitempty"`
	WorkspaceID    string `json:"workspace_id,omitempty"`
	ActiveRunID    string `json:"active_run_id,omitempty"`
	CreatedAt      string `json:"created_at"`
	UpdatedAt      string `json:"updated_at"`
}

func (c *Client) ListTasks(ctx context.Context, limit, offset int) (*ListTasksResponse, error) {
	path := fmt.Sprintf("/internal/tasks?limit=%d&offset=%d", limit, offset)
	var resp apiResponse
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data ListTasksResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

type RunHistoryResponse struct {
	Run      RunHistoryDTO `json:"run"`
	Task     TaskItem      `json:"task"`
	Events   []EventItem   `json:"events"`
	Messages []MessageDTO  `json:"messages"`
}

type RunHistoryDTO struct {
	ID        string `json:"id"`
	TaskID    string `json:"task_id"`
	Status    string `json:"status"`
	Version   int    `json:"version"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

type EventItem struct {
	ID        string                 `json:"id"`
	EventID   string                 `json:"event_id"`
	Type      string                 `json:"type"`
	RunID     string                 `json:"run_id,omitempty"`
	TaskID    string                 `json:"task_id,omitempty"`
	StepID    string                 `json:"step_id,omitempty"`
	Sequence  int64                  `json:"sequence"`
	Payload   map[string]interface{} `json:"payload"`
	Timestamp string                 `json:"timestamp"`
	CreatedAt string                 `json:"created_at"`
}

type ArtifactDTO struct {
	ID            string                 `json:"id"`
	TaskID        string                 `json:"task_id"`
	RunID         string                 `json:"run_id"`
	Kind          string                 `json:"kind"`
	Title         string                 `json:"title"`
	Purpose       string                 `json:"purpose"`
	Producer      ArtifactProducerDTO    `json:"producer"`
	Content       string                 `json:"content"`
	FileSizeBytes int64                  `json:"file_size_bytes,omitempty"`
	MimeType      string                 `json:"mime_type,omitempty"`
	ContentHash   string                 `json:"content_hash,omitempty"`
	Metadata      map[string]interface{} `json:"metadata,omitempty"`
	CreatedAt     string                 `json:"created_at"`
}

type ArtifactProducerDTO struct {
	Type       string `json:"type"`
	ToolCallID string `json:"tool_call_id,omitempty"`
}

type GetArtifactResponse struct {
	Artifact ArtifactDTO `json:"artifact"`
}

type StorageReconciliationIssueDTO struct {
	Code       string `json:"code"`
	Severity   string `json:"severity"`
	EntityType string `json:"entity_type"`
	EntityID   string `json:"entity_id"`
	Summary    string `json:"summary"`
	TaskID     string `json:"task_id,omitempty"`
	RunID      string `json:"run_id,omitempty"`
}

type StorageReconciliationResponse struct {
	Status           string                          `json:"status"`
	GeneratedAt      string                          `json:"generated_at"`
	ScannedRuns      int                             `json:"scanned_runs"`
	ScannedEvents    int                             `json:"scanned_events"`
	ScannedSteps     int                             `json:"scanned_steps"`
	ScannedArtifacts int                             `json:"scanned_artifacts"`
	IssueCount       int                             `json:"issue_count"`
	Truncated        bool                            `json:"truncated"`
	Issues           []StorageReconciliationIssueDTO `json:"issues"`
}

type TerminalEventRepairInspectionResponse struct {
	Eligible             bool     `json:"eligible"`
	ReasonCode           string   `json:"reason_code"`
	Reason               string   `json:"reason"`
	TaskID               string   `json:"task_id,omitempty"`
	RunID                string   `json:"run_id"`
	ExpectedEventType    string   `json:"expected_event_type,omitempty"`
	RiskLevel            string   `json:"risk_level"`
	RequiresConfirmation bool     `json:"requires_confirmation"`
	AllowedDecisions     []string `json:"allowed_decisions"`
}

type TerminalEventRepairRequestResponse struct {
	Request PermissionRequestDTO `json:"request"`
}

type TerminalEventRepairResolutionResponse struct {
	Request           PermissionRequestDTO `json:"request"`
	RepairedEventID   string               `json:"repaired_event_id,omitempty"`
	RepairedEventType string               `json:"repaired_event_type,omitempty"`
}

func (c *Client) GetStorageReconciliation(ctx context.Context, limit int) (*StorageReconciliationResponse, error) {
	path := fmt.Sprintf("/internal/runtime/storage-reconciliation?limit=%d", limit)
	var resp apiResponse
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data StorageReconciliationResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析业务真源对账响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) InspectTerminalEventRepair(ctx context.Context, runID string) (*TerminalEventRepairInspectionResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/runtime/storage-reconciliation/repairs/inspect", map[string]string{"run_id": runID}, &resp); err != nil {
		return nil, err
	}
	var data TerminalEventRepairInspectionResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析终态事件修复检查响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) CreateTerminalEventRepairRequest(ctx context.Context, runID string) (*TerminalEventRepairRequestResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/runtime/storage-reconciliation/repairs/requests", map[string]string{"run_id": runID}, &resp); err != nil {
		return nil, err
	}
	var data TerminalEventRepairRequestResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析终态事件修复请求响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ResolveTerminalEventRepairRequest(ctx context.Context, requestID, decision, note string) (*TerminalEventRepairResolutionResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/runtime/storage-reconciliation/repairs/requests/%s/resolve", requestID)
	if err := c.post(ctx, path, map[string]string{"decision": decision, "note": note}, &resp); err != nil {
		return nil, err
	}
	var data TerminalEventRepairResolutionResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析终态事件修复结果失败: %w", err)
	}
	return &data, nil
}

func (c *Client) GetArtifact(ctx context.Context, artifactID string) (*GetArtifactResponse, error) {
	req, err := http.NewRequestWithContext(
		ctx, http.MethodGet, c.baseURL+"/internal/artifacts/"+artifactID, nil,
	)
	if err != nil {
		return nil, fmt.Errorf("创建请求失败: %w", err)
	}
	if traceID := observability.TraceIDFromContext(ctx); traceID != "" {
		req.Header.Set("X-Trace-ID", traceID)
	}
	if requestID := observability.RequestIDFromContext(ctx); requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	var resp apiResponse
	if err := c.doWithLimit(req, &resp, c.httpClient, 12<<20); err != nil {
		return nil, err
	}
	var data GetArtifactResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析 Artifact 响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) GetRunHistory(ctx context.Context, runID string) (*RunHistoryResponse, error) {
	path := fmt.Sprintf("/internal/runs/%s/history", runID)
	var resp apiResponse
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data RunHistoryResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

type TaskHistoryResponse struct {
	Task     TaskItem     `json:"task"`
	Runs     []RunItem    `json:"runs"`
	Events   []EventItem  `json:"events"`
	Messages []MessageDTO `json:"messages"`
}

type RunItem struct {
	ID      string `json:"id"`
	Status  string `json:"status"`
	Version int    `json:"version"`
}

func (c *Client) GetTaskHistory(ctx context.Context, taskID string) (*TaskHistoryResponse, error) {
	path := fmt.Sprintf("/internal/tasks/%s/history", taskID)
	var resp apiResponse
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data TaskHistoryResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}
