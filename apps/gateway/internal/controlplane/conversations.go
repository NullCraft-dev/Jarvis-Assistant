package controlplane

import (
	"context"
	"encoding/json"
	"fmt"
	observability "github.com/jarvis-assistant/gateway/internal/observability"
	"net/http"
	"net/url"
)

// ── 会话（多轮对话 MVP）──

type ListConversationsResponse struct {
	Conversations []ConversationItem `json:"conversations"`
}

type ConversationItem struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at"`
}

func (c *Client) ListConversations(ctx context.Context, limit, offset int) (*ListConversationsResponse, error) {
	path := fmt.Sprintf("/internal/conversations?limit=%d&offset=%d", limit, offset)
	var resp apiResponse
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data ListConversationsResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析会话列表响应失败: %w", err)
	}
	return &data, nil
}

type GetConversationResponse struct {
	Conversation ConversationItem `json:"conversation"`
	Messages     []MessageDTO     `json:"messages"`
	NextCursor   *string          `json:"next_cursor,omitempty"`
}

func (c *Client) GetConversation(ctx context.Context, conversationID string, limit int, before string) (*GetConversationResponse, error) {
	u, err := url.Parse(fmt.Sprintf("%s/internal/conversations/%s", c.baseURL, conversationID))
	if err != nil {
		return nil, fmt.Errorf("构造会话请求 URL 失败: %w", err)
	}
	q := u.Query()
	if limit > 0 {
		q.Set("limit", fmt.Sprintf("%d", limit))
	}
	if before != "" {
		q.Set("before", before)
	}
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
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
	if err := c.do(req, &resp); err != nil {
		return nil, err
	}
	var data GetConversationResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析会话详情响应失败: %w", err)
	}
	return &data, nil
}
