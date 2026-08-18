package handlers

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/controlplane"
	"github.com/jarvis-assistant/gateway/internal/orchestrator"
	"github.com/jarvis-assistant/gateway/internal/redis"
)

type fakeRuntimeHealthProvider struct{ health orchestrator.RuntimeHealth }

func (f fakeRuntimeHealthProvider) GetRuntimeHealth(context.Context) orchestrator.RuntimeHealth {
	return f.health
}

type fakeRuntimeDiagnosticsProvider struct {
	fakeRuntimeHealthProvider
	page   redis.DeadLetterPage
	record *redis.DeadLetterRecord
}

func (f fakeRuntimeDiagnosticsProvider) GetRuntimeDeadLetter(context.Context, string, string) (*redis.DeadLetterRecord, error) {
	return f.record, nil
}

func (f fakeRuntimeDiagnosticsProvider) ListRuntimeDeadLetters(context.Context, string, int, string, string, string, string) (redis.DeadLetterPage, error) {
	return f.page, nil
}

func TestRuntimeHealthHandlerReturnsSafeProjection(t *testing.T) {
	h := NewRuntimeHealthHandler(fakeRuntimeHealthProvider{health: orchestrator.RuntimeHealth{Status: "healthy", RuntimeBus: "redis", GeneratedAt: "2026-07-22T10:00:00Z", Warnings: []string{}}}, nil)
	rec := httptest.NewRecorder()
	h.GetRuntimeHealth(rec, httptest.NewRequest(http.MethodGet, "/api/runtime/health", nil))
	var response map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if rec.Code != http.StatusOK || response["ok"] != true {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}
}

func TestRuntimeHealthHandlerInMemoryFallback(t *testing.T) {
	h := NewRuntimeHealthHandler(nil, nil)
	rec := httptest.NewRecorder()
	h.GetRuntimeHealth(rec, httptest.NewRequest(http.MethodGet, "/api/runtime/health", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("unexpected status: %d", rec.Code)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "unavailable") || !strings.Contains(body, "inmemory") {
		t.Fatalf("unexpected body: %s", body)
	}
}

func TestStorageReconciliationProxiesBoundedSafeResult(t *testing.T) {
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.Path != "/internal/runtime/storage-reconciliation" || r.URL.Query().Get("limit") != "25" {
			t.Fatalf("unexpected target: %s", r.URL.String())
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body: io.NopCloser(strings.NewReader(
				`{"ok":true,"data":{"status":"degraded","generated_at":"2026-07-24T00:00:00Z","scanned_runs":1,"scanned_events":2,"scanned_steps":1,"scanned_artifacts":0,"issue_count":1,"truncated":false,"issues":[{"code":"EVENT_SEQUENCE_GAP","severity":"error","entity_type":"run","entity_id":"run-1","summary":"RuntimeEvent 序号不连续","task_id":"task-1","run_id":"run-1"}]}}`,
			)),
		}, nil
	})}
	h := NewRuntimeHealthHandler(nil, controlplane.NewClientWithHTTPClient("http://control-plane", httpClient))
	recorder := httptest.NewRecorder()
	h.GetStorageReconciliation(recorder, httptest.NewRequest(
		http.MethodGet, "/api/runtime/storage-reconciliation?limit=25", nil,
	))
	body := recorder.Body.String()
	if recorder.Code != http.StatusOK || !strings.Contains(body, "EVENT_SEQUENCE_GAP") {
		t.Fatalf("unexpected response: %d %s", recorder.Code, body)
	}
	if strings.Contains(body, "file_path") || strings.Contains(body, "content") {
		t.Fatalf("unsafe field leaked: %s", body)
	}
}

func TestStorageReconciliationRejectsInvalidLimit(t *testing.T) {
	h := NewRuntimeHealthHandler(nil, nil)
	for _, target := range []string{
		"/api/runtime/storage-reconciliation?limit=0",
		"/api/runtime/storage-reconciliation?limit=101",
		"/api/runtime/storage-reconciliation?limit=bad",
	} {
		recorder := httptest.NewRecorder()
		h.GetStorageReconciliation(recorder, httptest.NewRequest(http.MethodGet, target, nil))
		if recorder.Code != http.StatusBadRequest {
			t.Fatalf("%s: expected 400, got %d %s", target, recorder.Code, recorder.Body.String())
		}
	}
}

func TestTerminalEventRepairInspectionValidatesRunAndProxiesSafeResponse(t *testing.T) {
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.Path != "/internal/runtime/storage-reconciliation/repairs/inspect" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		var input map[string]string
		if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
			t.Fatal(err)
		}
		if input["run_id"] != "00000000-0000-4000-8000-000000000002" {
			t.Fatalf("unexpected input: %#v", input)
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body: io.NopCloser(strings.NewReader(
				`{"ok":true,"data":{"eligible":true,"reason_code":"TERMINAL_EVENT_REPAIR_ELIGIBLE","reason":"eligible","task_id":"00000000-0000-4000-8000-000000000001","run_id":"00000000-0000-4000-8000-000000000002","expected_event_type":"agent.run.failed","risk_level":"L3","requires_confirmation":true,"allowed_decisions":["allow_once","deny"]}}`,
			)),
		}, nil
	})}
	h := NewRuntimeHealthHandler(nil, controlplane.NewClientWithHTTPClient("http://control-plane", httpClient))
	recorder := httptest.NewRecorder()
	h.InspectTerminalEventRepair(recorder, httptest.NewRequest(
		http.MethodPost, "/api/runtime/storage-reconciliation/repairs/inspect",
		strings.NewReader(`{"run_id":"00000000-0000-4000-8000-000000000002"}`),
	))
	if recorder.Code != http.StatusOK || !strings.Contains(recorder.Body.String(), "TERMINAL_EVENT_REPAIR_ELIGIBLE") {
		t.Fatalf("unexpected response: %d %s", recorder.Code, recorder.Body.String())
	}

	invalid := httptest.NewRecorder()
	h.InspectTerminalEventRepair(invalid, httptest.NewRequest(
		http.MethodPost, "/api/runtime/storage-reconciliation/repairs/inspect",
		strings.NewReader(`{"run_id":"not-a-uuid"}`),
	))
	if invalid.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d %s", invalid.Code, invalid.Body.String())
	}
}

func TestRuntimeDeadLetterHandlerReturnsWhitelistPage(t *testing.T) {
	h := NewRuntimeHealthHandler(fakeRuntimeDiagnosticsProvider{page: redis.DeadLetterPage{
		Records:    []redis.DeadLetterRecord{{ID: "1-0", Source: "run_queue", ErrorCode: "RUN_QUEUE_MALFORMED"}},
		NextCursor: "1-0",
	}}, nil)
	rec := httptest.NewRecorder()
	h.ListDeadLetters(rec, httptest.NewRequest(http.MethodGet, "/api/runtime/dead-letters?source=run_queue&limit=20", nil))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "RUN_QUEUE_MALFORMED") {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}
}

func TestRuntimeDeadLetterHandlerRejectsInvalidFilters(t *testing.T) {
	h := NewRuntimeHealthHandler(fakeRuntimeDiagnosticsProvider{}, nil)
	for _, target := range []string{
		"/api/runtime/dead-letters?source=unknown",
		"/api/runtime/dead-letters?limit=51",
		"/api/runtime/dead-letters?before=bad-cursor",
		"/api/runtime/dead-letters?error_code=bad%20code",
		"/api/runtime/dead-letters?task_id=not-a-uuid",
	} {
		rec := httptest.NewRecorder()
		h.ListDeadLetters(rec, httptest.NewRequest(http.MethodGet, target, nil))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: expected 400, got %d %s", target, rec.Code, rec.Body.String())
		}
	}
}

func TestRuntimeDeadLetterRetryInspectionUsesExactSafeRecord(t *testing.T) {
	received := map[string]interface{}{}
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.URL.Path != "/internal/runtime/dlq-retry/inspect" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
			t.Fatal(err)
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(strings.NewReader(`{"ok":true,"data":{"eligible":true,"reason_code":"DLQ_RETRY_ELIGIBLE","reason":"eligible","task_id":"00000000-0000-4000-8000-000000000001","run_id":"00000000-0000-4000-8000-000000000002","risk_level":"L3","requires_confirmation":true,"allowed_decisions":["allow_once","deny"]}}`)),
		}, nil
	})}

	h := NewRuntimeHealthHandler(fakeRuntimeDiagnosticsProvider{record: &redis.DeadLetterRecord{
		ID: "10-0", Source: "run_queue", OriginalMessageID: "9-0",
		ErrorCode:     "RUN_QUEUE_RETRY_EXHAUSTED",
		TaskID:        "00000000-0000-4000-8000-000000000001",
		RunID:         "00000000-0000-4000-8000-000000000002",
		PayloadSHA256: strings.Repeat("a", 64),
	}}, controlplane.NewClientWithHTTPClient("http://control-plane", httpClient))
	recorder := httptest.NewRecorder()
	h.InspectDeadLetterRetry(recorder, httptest.NewRequest(
		http.MethodPost, "/api/runtime/dead-letters/retry/inspect",
		strings.NewReader(`{"source":"run_queue","record_id":"10-0"}`),
	))
	if recorder.Code != http.StatusOK || !strings.Contains(recorder.Body.String(), "DLQ_RETRY_ELIGIBLE") {
		t.Fatalf("unexpected response: %d %s", recorder.Code, recorder.Body.String())
	}
	if received["record_id"] != "10-0" || received["payload"] != nil || received["user_goal"] != nil {
		t.Fatalf("unsafe or incomplete evidence: %#v", received)
	}
}

func TestRuntimeDeadLetterRetryResolveRejectsUnsupportedDecision(t *testing.T) {
	h := NewRuntimeHealthHandler(fakeRuntimeDiagnosticsProvider{}, controlplane.NewClient("http://control-plane.invalid"))
	recorder := httptest.NewRecorder()
	h.ResolveDeadLetterRetryRequest(
		recorder,
		httptest.NewRequest(http.MethodPost, "/resolve", strings.NewReader(`{"decision":"always_allow_for_workspace"}`)),
		"00000000-0000-4000-8000-000000000001",
	)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d %s", recorder.Code, recorder.Body.String())
	}
}
