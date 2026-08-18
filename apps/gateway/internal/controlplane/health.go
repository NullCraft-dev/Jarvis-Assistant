package controlplane

import (
	"context"
	"encoding/json"
)

// ── 健康检查 ──

type HealthResponse struct {
	Status          string `json:"status"`
	Database        string `json:"database"`
	Redis           string `json:"redis"`
	OutboxPublisher string `json:"outbox_publisher"`
}

func (c *Client) HealthCheck(ctx context.Context) (*HealthResponse, error) {
	var resp apiResponse
	if err := c.get(ctx, "/internal/health", &resp); err != nil {
		return nil, err
	}
	var data HealthResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, err
	}
	return &data, nil
}
