package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
)

// ── Model Config（Phase 6）──

type ModelConfigResponse struct {
	Provider         string  `json:"provider"`
	Protocol         string  `json:"protocol"`
	ModelName        string  `json:"model_name"`
	BaseURLDisplay   string  `json:"base_url_display"`
	APIKeyConfigured bool    `json:"api_key_configured"`
	TimeoutSeconds   int     `json:"timeout_seconds"`
	MaxRetries       int     `json:"max_retries"`
	MaxTokens        int     `json:"max_tokens"`
	ThinkingMode     string  `json:"thinking_mode"`
	WorkerStatus     string  `json:"worker_status"`
	LastHeartbeatAt  *string `json:"last_heartbeat_at"`
	LastErrorCode    *string `json:"last_error_code"`
}

type ModelTestResponse struct {
	Provider  string          `json:"provider"`
	Model     string          `json:"model"`
	LatencyMs float64         `json:"latency_ms"`
	TestedAt  string          `json:"tested_at"`
	Status    string          `json:"status"` // "ok" | "failed"
	Error     *ModelTestError `json:"error"`
}

type ModelTestError struct {
	Code        string `json:"code"`
	Message     string `json:"message"`
	Category    string `json:"category"`
	Recoverable bool   `json:"recoverable"`
}

func (c *Client) GetModelConfig(ctx context.Context) (*ModelConfigResponse, error) {
	var resp apiResponse
	if err := c.get(ctx, "/internal/model-config", &resp); err != nil {
		return nil, err
	}
	var data ModelConfigResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析模型配置响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) TestModelConnection(ctx context.Context) (*ModelTestResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/model-config/test", nil, &resp); err != nil {
		return nil, err
	}
	var data ModelTestResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析模型测试响应失败: %w", err)
	}
	return &data, nil
}
