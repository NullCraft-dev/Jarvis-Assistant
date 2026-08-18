package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
)

// ── DLQ 受控重试 ──

type DlqRetryEvidenceRequest struct {
	Source            string `json:"source"`
	RecordID          string `json:"record_id"`
	OriginalMessageID string `json:"original_message_id"`
	ErrorCode         string `json:"error_code"`
	TaskID            string `json:"task_id"`
	RunID             string `json:"run_id"`
	PayloadSHA256     string `json:"payload_sha256,omitempty"`
}

type DlqRetryInspectionResponse struct {
	Eligible             bool     `json:"eligible"`
	ReasonCode           string   `json:"reason_code"`
	Reason               string   `json:"reason"`
	TaskID               string   `json:"task_id"`
	RunID                string   `json:"run_id"`
	RiskLevel            string   `json:"risk_level"`
	RequiresConfirmation bool     `json:"requires_confirmation"`
	AllowedDecisions     []string `json:"allowed_decisions"`
}

type DlqRetryRequestResponse struct {
	Request PermissionRequestDTO `json:"request"`
}

type DlqRetryResolutionResponse struct {
	Request       PermissionRequestDTO `json:"request"`
	PreviousRunID string               `json:"previous_run_id"`
	NewRun        *RunDTO              `json:"new_run,omitempty"`
}

func (c *Client) InspectDlqRetry(ctx context.Context, input DlqRetryEvidenceRequest) (*DlqRetryInspectionResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/runtime/dlq-retry/inspect", input, &resp); err != nil {
		return nil, err
	}
	var data DlqRetryInspectionResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) CreateDlqRetryRequest(ctx context.Context, input DlqRetryEvidenceRequest) (*DlqRetryRequestResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/runtime/dlq-retry/requests", input, &resp); err != nil {
		return nil, err
	}
	var data DlqRetryRequestResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ResolveDlqRetryRequest(ctx context.Context, requestID, decision, note string) (*DlqRetryResolutionResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/runtime/dlq-retry/requests/%s/resolve", requestID)
	if err := c.post(ctx, path, map[string]string{"decision": decision, "note": note}, &resp); err != nil {
		return nil, err
	}
	var data DlqRetryResolutionResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析响应失败: %w", err)
	}
	return &data, nil
}
