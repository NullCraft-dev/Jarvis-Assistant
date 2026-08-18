package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
)

// ── 取消 + 权限 ──

type CancelRunResponse struct {
	RunID     string `json:"run_id"`
	TaskID    string `json:"task_id,omitempty"`
	AgentID   string `json:"agent_id,omitempty"`
	Mode      string `json:"mode,omitempty"`
	Status    string `json:"status"`
	Version   int    `json:"version"`
	CreatedAt string `json:"created_at,omitempty"`
	UpdatedAt string `json:"updated_at,omitempty"`
}

type ControlRunResponse = CancelRunResponse

func (c *Client) CancelRun(ctx context.Context, runID string, reason string) (*CancelRunResponse, error) {
	body := map[string]string{"reason": reason}
	var resp apiResponse
	if err := c.post(ctx, fmt.Sprintf("/internal/runs/%s/cancel", runID), body, &resp); err != nil {
		return nil, err
	}
	var data CancelRunResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) PauseRun(ctx context.Context, runID string, reason string) (*ControlRunResponse, error) {
	body := map[string]string{"reason": reason}
	var resp apiResponse
	if err := c.post(ctx, fmt.Sprintf("/internal/runs/%s/pause", runID), body, &resp); err != nil {
		return nil, err
	}
	var data ControlRunResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ResumeRun(ctx context.Context, runID string) (*ControlRunResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, fmt.Sprintf("/internal/runs/%s/resume", runID), struct{}{}, &resp); err != nil {
		return nil, err
	}
	var data ControlRunResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) RetryFailedStep(ctx context.Context, runID string, stepID string) (*ControlRunResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/runs/%s/steps/%s/retry", runID, stepID)
	if err := c.post(ctx, path, struct{}{}, &resp); err != nil {
		return nil, err
	}
	var data ControlRunResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

type PermissionDecisionRequest struct {
	RequestID string `json:"request_id"`
	Decision  string `json:"decision"`
	Note      string `json:"note"`
}

type PermissionDecisionResponse struct {
	Request PermissionRequestDTO `json:"request"`
	Events  []EventItem          `json:"events"`
}

type PermissionRequestDTO struct {
	ID               string                 `json:"id"`
	TaskID           string                 `json:"task_id"`
	RunID            string                 `json:"run_id"`
	StepID           string                 `json:"step_id,omitempty"`
	ToolName         string                 `json:"tool_name"`
	ActionSummary    string                 `json:"action_summary"`
	Reason           string                 `json:"reason,omitempty"`
	RiskLevel        string                 `json:"risk_level"`
	Scope            map[string]interface{} `json:"scope"`
	ArgumentsSummary map[string]interface{} `json:"arguments_summary"`
	AllowedDecisions []string               `json:"allowed_decisions"`
	CreatedAt        string                 `json:"created_at"`
	ExpiresAt        string                 `json:"expires_at"`
	Status           string                 `json:"status"`
	Decision         string                 `json:"decision,omitempty"`
}

func (c *Client) ResolvePermission(ctx context.Context, req PermissionDecisionRequest) (*PermissionDecisionResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/permissions/decide", req, &resp); err != nil {
		return nil, err
	}
	var data PermissionDecisionResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

type PendingPermissionsResponse struct {
	Requests []PermissionRequestDTO `json:"requests"`
}

func (c *Client) ListPendingPermissions(ctx context.Context, runID string) (*PendingPermissionsResponse, error) {
	var resp apiResponse
	if err := c.get(ctx, fmt.Sprintf("/internal/runs/%s/permissions", runID), &resp); err != nil {
		return nil, err
	}
	var data PendingPermissionsResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析待处理权限响应失败: %w", err)
	}
	return &data, nil
}
