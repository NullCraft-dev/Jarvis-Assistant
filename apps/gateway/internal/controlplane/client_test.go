package controlplane

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	observability "github.com/jarvis-assistant/gateway/internal/observability"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func TestGetPropagatesTraceIDFromContext(t *testing.T) {
	client := NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if got := r.Header.Get("X-Trace-ID"); got != "trace-01" {
				t.Fatalf("trace id 未透传到 Control Plane: %q", got)
			}
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     make(http.Header),
				Body:       io.NopCloser(strings.NewReader(`{"ok":true,"data":{"status":"ok"}}`)),
			}, nil
		}),
	})

	ctx := observability.WithTraceID(context.Background(), "trace-01")
	if _, err := client.HealthCheck(ctx); err != nil {
		t.Fatalf("HealthCheck 失败: %v", err)
	}
}

func TestGetPropagatesRequestIDFromContext(t *testing.T) {
	client := NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if got := r.Header.Get("X-Request-ID"); got != "request-01" {
				t.Fatalf("X-Request-ID 未透传: got=%q", got)
			}
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     make(http.Header),
				Body:       io.NopCloser(strings.NewReader(`{"ok":true,"data":{}}`)),
			}, nil
		}),
	})
	ctx := observability.WithRequestID(context.Background(), "request-01")
	var response apiResponse
	if err := client.get(ctx, "/test", &response); err != nil {
		t.Fatalf("get 返回错误: %v", err)
	}
}

func TestPauseAndResumeRunUseDedicatedControlPlaneEndpoints(t *testing.T) {
	requests := make([]string, 0, 2)
	client := NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			requests = append(requests, r.URL.Path)
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     make(http.Header),
				Body: io.NopCloser(strings.NewReader(
					`{"ok":true,"data":{"run_id":"run-1","status":"paused","version":3}}`,
				)),
			}, nil
		}),
	})

	pause, err := client.PauseRun(context.Background(), "run-1", "user request")
	if err != nil || pause.RunID != "run-1" {
		t.Fatalf("PauseRun 失败: response=%+v err=%v", pause, err)
	}
	resume, err := client.ResumeRun(context.Background(), "run-1")
	if err != nil || resume.RunID != "run-1" {
		t.Fatalf("ResumeRun 失败: response=%+v err=%v", resume, err)
	}
	if strings.Join(requests, ",") != "/internal/runs/run-1/pause,/internal/runs/run-1/resume" {
		t.Fatalf("Control Plane 路径错误: %v", requests)
	}
}

func TestRetryFailedStepUsesExactRunAndStepPath(t *testing.T) {
	var requested string
	client := NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			requested = r.URL.Path
			return &http.Response{
				StatusCode: http.StatusOK, Header: make(http.Header),
				Body: io.NopCloser(strings.NewReader(
					`{"ok":true,"data":{"run_id":"replacement-1","status":"queued","version":1}}`,
				)),
			}, nil
		}),
	})

	result, err := client.RetryFailedStep(context.Background(), "run-1", "step-2")
	if err != nil || result.RunID != "replacement-1" {
		t.Fatalf("RetryFailedStep 失败: response=%+v err=%v", result, err)
	}
	if requested != "/internal/runs/run-1/steps/step-2/retry" {
		t.Fatalf("Control Plane 路径错误: %s", requested)
	}
}
