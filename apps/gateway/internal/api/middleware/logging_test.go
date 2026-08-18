package middleware

import (
	"bytes"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/google/uuid"
	observability "github.com/jarvis-assistant/gateway/internal/observability"
)

func TestLoggingRejectsUnsafeTraceIDAndBindsGeneratedTrace(t *testing.T) {
	var seenTrace string
	var seenRequest string
	handler := Logging(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenTrace = observability.TraceIDFromContext(r.Context())
		seenRequest = observability.RequestIDFromContext(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodGet, "/api/health", nil)
	req.Header.Set("X-Trace-ID", "trace | ERROR | forged")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if seenTrace == "" || seenTrace == "trace | ERROR | forged" {
		t.Fatalf("不安全 trace id 未被替换: %q", seenTrace)
	}
	if got := rec.Header().Get("X-Trace-ID"); got != seenTrace {
		t.Fatalf("响应未返回实际 trace id: got=%q want=%q", got, seenTrace)
	}
	if seenRequest == "" {
		t.Fatal("request id 未写入 context")
	}
	if got := rec.Header().Get("X-Request-ID"); got != seenRequest {
		t.Fatalf("响应未返回实际 request id: got=%q want=%q", got, seenRequest)
	}
}

func TestLoggingPreservesSSEFlusher(t *testing.T) {
	flusherAvailable := false
	handler := Logging(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, flusherAvailable = w.(http.Flusher)
		if flusher, ok := w.(http.Flusher); ok {
			flusher.Flush()
		}
	}))

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/runs/run-1/events", nil))

	if !flusherAvailable {
		t.Fatal("Logging middleware 包装后必须保留 http.Flusher，SSE 才能工作")
	}
}

func TestLoggingReplacesNonUUIDTraceForRunContinuity(t *testing.T) {
	var seenTrace string
	handler := Logging(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenTrace = observability.TraceIDFromContext(r.Context())
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodPost, "/api/tasks", nil)
	req.Header.Set("X-Trace-ID", "safe-but-not-a-uuid")
	req.Header.Set("X-Request-ID", "request-01")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if _, err := uuid.Parse(seenTrace); err != nil {
		t.Fatalf("run trace 必须规范化为 UUID: trace=%q err=%v", seenTrace, err)
	}
}

func TestLoggingKeepsSuccessAtDebugAndServerErrorsAtError(t *testing.T) {
	var output bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(
		&output, &slog.HandlerOptions{Level: slog.LevelInfo},
	)))
	defer slog.SetDefault(previous)

	success := Logging(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	success.ServeHTTP(
		httptest.NewRecorder(),
		httptest.NewRequest(http.MethodGet, "/api/tasks", nil),
	)
	if strings.Contains(output.String(), "status=204") {
		t.Fatalf("successful request must not appear at INFO: %s", output.String())
	}

	failure := Logging(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	failure.ServeHTTP(
		httptest.NewRecorder(),
		httptest.NewRequest(http.MethodGet, "/api/tasks", nil),
	)
	if line := output.String(); !strings.Contains(line, "level=ERROR") ||
		!strings.Contains(line, "status=503") {
		t.Fatalf("server error must appear at ERROR: %s", line)
	}
}
