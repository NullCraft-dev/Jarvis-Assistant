package controlplane

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	observability "github.com/jarvis-assistant/gateway/internal/observability"
)

// Client 是 Python Control Plane 的 HTTP client。
type Client struct {
	baseURL          string
	httpClient       *http.Client // 普通请求（list/create/cancel/revoke/health），默认 10s
	pickerHTTPClient *http.Client // picker 请求，默认 75s（> macOS 60s + buffer）
	streamHTTPClient *http.Client // 有界导出流，默认 120s
}

func NewClient(baseURL string) *Client {
	return &Client{
		baseURL:          baseURL,
		httpClient:       &http.Client{Timeout: 10 * time.Second},
		pickerHTTPClient: &http.Client{Timeout: 75 * time.Second},
		streamHTTPClient: &http.Client{Timeout: 120 * time.Second},
	}
}

// NewClientWithHTTPClient 允许测试注入无网络 RoundTripper。
func NewClientWithHTTPClient(baseURL string, httpClient *http.Client) *Client {
	return &Client{
		baseURL:          baseURL,
		httpClient:       httpClient,
		pickerHTTPClient: httpClient,
		streamHTTPClient: httpClient,
	}
}

// ── 通用响应 ──

type apiResponse struct {
	Ok    bool            `json:"ok"`
	Data  json.RawMessage `json:"data,omitempty"`
	Error *apiError       `json:"error,omitempty"`
}

type apiError struct {
	Code        string `json:"code"`
	Message     string `json:"message"`
	Category    string `json:"category"`
	Recoverable bool   `json:"recoverable"`
}

// ControlPlaneError Python Control Plane 返回的错误。
type ControlPlaneError struct {
	Code        string
	Message     string
	Category    string
	Recoverable bool
}

func (e *ControlPlaneError) Error() string {
	return fmt.Sprintf("[%s] %s (category=%s, recoverable=%v)", e.Code, e.Message, e.Category, e.Recoverable)
}

// ── HTTP 方法 ──

func (c *Client) post(ctx context.Context, path string, body interface{}, result *apiResponse) error {
	return c.postWithClient(ctx, path, body, result, c.httpClient)
}

func (c *Client) sendJSON(ctx context.Context, method, path string, body interface{}, result *apiResponse) error {
	data, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("序列化请求失败: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("创建请求失败: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if traceID := observability.TraceIDFromContext(ctx); traceID != "" {
		req.Header.Set("X-Trace-ID", traceID)
	}
	if requestID := observability.RequestIDFromContext(ctx); requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	return c.do(req, result)
}

func (c *Client) postWithClient(ctx context.Context, path string, body interface{}, result *apiResponse, client *http.Client) error {
	data, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("序列化请求失败: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("创建请求失败: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if traceID := observability.TraceIDFromContext(ctx); traceID != "" {
		req.Header.Set("X-Trace-ID", traceID)
	}
	if requestID := observability.RequestIDFromContext(ctx); requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	return c.doWithClient(req, result, client)
}

func (c *Client) get(ctx context.Context, path string, result *apiResponse) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+path, nil)
	if err != nil {
		return fmt.Errorf("创建请求失败: %w", err)
	}
	if traceID := observability.TraceIDFromContext(ctx); traceID != "" {
		req.Header.Set("X-Trace-ID", traceID)
	}
	if requestID := observability.RequestIDFromContext(ctx); requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	return c.do(req, result)
}

func (c *Client) do(req *http.Request, result *apiResponse) error {
	return c.doWithClient(req, result, c.httpClient)
}

func (c *Client) doWithClient(req *http.Request, result *apiResponse, client *http.Client) error {
	return c.doWithLimit(req, result, client, 1<<20)
}

func (c *Client) doWithLimit(req *http.Request, result *apiResponse, client *http.Client, maxBytes int64) error {
	startedAt := time.Now()
	resp, err := client.Do(req)
	if err != nil {
		slog.ErrorContext(req.Context(), "Control Plane 请求失败",
			"method", req.Method,
			"path", req.URL.Path,
			"duration_ms", time.Since(startedAt).Milliseconds(),
			"error_type", fmt.Sprintf("%T", err),
		)
		return fmt.Errorf("Control Plane 请求失败: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes))
	if err != nil {
		slog.ErrorContext(req.Context(), "Control Plane 响应读取失败",
			"method", req.Method,
			"path", req.URL.Path,
			"status", resp.StatusCode,
			"duration_ms", time.Since(startedAt).Milliseconds(),
		)
		return fmt.Errorf("读取响应失败: %w", err)
	}
	if err := json.Unmarshal(body, result); err != nil {
		slog.ErrorContext(req.Context(), "Control Plane 响应解析失败",
			"method", req.Method,
			"path", req.URL.Path,
			"status", resp.StatusCode,
			"duration_ms", time.Since(startedAt).Milliseconds(),
		)
		return fmt.Errorf("解析响应 JSON 失败 (body=%s): %w", string(body[:minInt(len(body), 200)]), err)
	}
	if !result.Ok && result.Error != nil {
		slog.WarnContext(req.Context(), "Control Plane 返回业务错误",
			"method", req.Method,
			"path", req.URL.Path,
			"status", resp.StatusCode,
			"error_code", result.Error.Code,
			"recoverable", result.Error.Recoverable,
			"duration_ms", time.Since(startedAt).Milliseconds(),
		)
		return &ControlPlaneError{
			Code:        result.Error.Code,
			Message:     result.Error.Message,
			Category:    result.Error.Category,
			Recoverable: result.Error.Recoverable,
		}
	}
	if !isPollingPath(req.URL.Path) {
		slog.DebugContext(req.Context(), "Control Plane 请求完成",
			"method", req.Method,
			"path", req.URL.Path,
			"status", resp.StatusCode,
			"duration_ms", time.Since(startedAt).Milliseconds(),
		)
	}
	return nil
}

// doStream 校验 Control Plane 状态后把响应 body 所有权交给调用方。
// 只有审计导出这类自身已有严格字节预算的端点可以使用。
func (c *Client) doStream(req *http.Request) (*http.Response, error) {
	resp, err := c.streamHTTPClient.Do(req)
	if err != nil {
		slog.ErrorContext(
			req.Context(),
			"Control Plane 流式请求失败",
			"method", req.Method,
			"path", req.URL.Path,
			"error_type", fmt.Sprintf("%T", err),
		)
		return nil, fmt.Errorf("Control Plane 流式请求失败: %w", err)
	}
	if resp.StatusCode >= http.StatusOK && resp.StatusCode < http.StatusMultipleChoices {
		return resp, nil
	}
	defer resp.Body.Close()

	var result apiResponse
	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if readErr == nil && json.Unmarshal(body, &result) == nil && result.Error != nil {
		return nil, &ControlPlaneError{
			Code:        result.Error.Code,
			Message:     result.Error.Message,
			Category:    result.Error.Category,
			Recoverable: result.Error.Recoverable,
		}
	}
	return nil, fmt.Errorf("Control Plane 流式请求返回状态 %d", resp.StatusCode)
}

func isPollingPath(path string) bool {
	switch path {
	case "/internal/health", "/internal/runtime/workers", "/internal/runtime/streams":
		return true
	default:
		return false
	}
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
