package handlers

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

func TestSubscribeEventsRestoresControlPlaneHistoryWithSSEID(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.URL.Path != "/internal/runs/run-1/history" {
				t.Fatalf("unexpected path: %s", r.URL.Path)
			}
			return jsonResponse(`{"ok":true,"data":{"run":{"id":"run-1","task_id":"task-1","status":"running","version":2,"created_at":"2026-07-14T00:00:00Z","updated_at":"2026-07-14T00:00:00Z"},"task":{"id":"task-1","conversation_id":"conv-1","title":"test","user_goal":"test","status":"running","created_at":"2026-07-14T00:00:00Z","updated_at":"2026-07-14T00:00:00Z"},"events":[{"id":"event-1","event_id":"event-1","type":"agent.run.started","task_id":"task-1","run_id":"run-1","sequence":2,"payload":{"agent_id":"worker-1"},"timestamp":"2026-07-14T00:00:01Z","created_at":"2026-07-14T00:00:01Z"}],"messages":[]}}`), nil
		}),
	})

	fb := newFakeBus()
	handler := NewRunHandler(fb, fb, cpClient)
	req := httptest.NewRequest(http.MethodGet, "/api/runs/run-1/events", nil)
	ctx, cancel := context.WithCancel(req.Context())
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		handler.SubscribeEvents(rec, req, "run-1")
		close(done)
	}()
	time.Sleep(50 * time.Millisecond)
	cancel()
	<-done

	body := rec.Body.String()
	if !strings.Contains(body, "id: event-1\n") {
		t.Fatalf("SSE missing event id: %s", body)
	}
	if !strings.Contains(body, `"payload":{"agent_id":"worker-1"}`) {
		t.Fatalf("SSE missing persisted payload: %s", body)
	}
}

func TestSubscribeEventsReconcilesPersistedHistoryAfterRealtimeProjectionGap(t *testing.T) {
	var historyCalls atomic.Int32
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.URL.Path != "/internal/runs/run-1/history" {
				t.Fatalf("unexpected path: %s", r.URL.Path)
			}
			events := `[{"id":"event-1","event_id":"event-1","type":"permission.required","task_id":"task-1","run_id":"run-1","sequence":1,"payload":{},"timestamp":"2026-07-30T10:00:00Z","created_at":"2026-07-30T10:00:00Z"}]`
			if historyCalls.Add(1) > 1 {
				events = `[` +
					`{"id":"event-1","event_id":"event-1","type":"permission.required","task_id":"task-1","run_id":"run-1","sequence":1,"payload":{},"timestamp":"2026-07-30T10:00:00Z","created_at":"2026-07-30T10:00:00Z"},` +
					`{"id":"event-2","event_id":"event-2","type":"permission.resolved","task_id":"task-1","run_id":"run-1","sequence":2,"payload":{"decision":"allow_once"},"timestamp":"2026-07-30T10:00:01Z","created_at":"2026-07-30T10:00:01Z"},` +
					`{"id":"event-3","event_id":"event-3","type":"agent.run.completed","task_id":"task-1","run_id":"run-1","sequence":3,"payload":{"output":"done"},"timestamp":"2026-07-30T10:00:02Z","created_at":"2026-07-30T10:00:02Z"}]`
			}
			return jsonResponse(`{"ok":true,"data":{"run":{"id":"run-1","task_id":"task-1","status":"running","version":2,"created_at":"2026-07-30T10:00:00Z","updated_at":"2026-07-30T10:00:00Z"},"task":{"id":"task-1","conversation_id":"conv-1","title":"test","user_goal":"test","status":"running","created_at":"2026-07-30T10:00:00Z","updated_at":"2026-07-30T10:00:00Z"},"events":` + events + `,"messages":[]}}`), nil
		}),
	})

	// 故意不向内存 RuntimeBus 追加恢复事件，模拟 Redis 实时投影短暂漏失。
	fb := newFakeBus()
	handler := NewRunHandler(fb, fb, cpClient)
	req := httptest.NewRequest(http.MethodGet, "/api/runs/run-1/events", nil)
	ctx, cancel := context.WithCancel(req.Context())
	req = req.WithContext(ctx)
	rec := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		handler.SubscribeEvents(rec, req, "run-1")
		close(done)
	}()

	time.Sleep(persistedHistoryPollInterval + 300*time.Millisecond)
	cancel()
	<-done

	body := rec.Body.String()
	if strings.Count(body, `"event-1"`) != 1 {
		t.Fatalf("persisted history reconciliation duplicated event-1: %s", body)
	}
	if !strings.Contains(body, `"event-2"`) || !strings.Contains(body, `"event-3"`) {
		t.Fatalf("SSE did not reconcile persisted permission recovery events: %s", body)
	}
	if historyCalls.Load() < 2 {
		t.Fatalf("expected initial history plus reconciliation, got %d calls", historyCalls.Load())
	}
}

func TestCreateTaskPassesAndReturnsConversation(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			var input controlplane.CreateTaskRequest
			if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
				t.Fatal(err)
			}
			if input.ConversationID != "conv-existing" {
				t.Fatalf("conversation_id not forwarded: %q", input.ConversationID)
			}
			if input.WorkspacePath != "/workspace" {
				t.Fatalf("workspace_path not forwarded: %q", input.WorkspacePath)
			}
			return jsonResponse(`{"ok":true,"data":{"task":{"id":"task-1","conversation_id":"conv-existing","title":"hello","user_goal":"hello","status":"running","workspace_path":"/workspace","active_run_id":"run-1","created_at":"2026-07-14T00:00:00Z","updated_at":"2026-07-14T00:00:00Z"},"run":{"id":"run-1","task_id":"task-1","agent_id":"default","mode":"single_agent","status":"queued","version":1,"created_at":"2026-07-14T00:00:00Z","updated_at":"2026-07-14T00:00:00Z"},"conversation":{"id":"conv-existing","title":"hello","created_at":"2026-07-14T00:00:00Z"},"message":{"id":"msg-1","role":"user","content":"hello","conversation_id":"conv-existing","task_id":"task-1","created_at":"2026-07-14T00:00:00Z"},"initial_event":{"id":"row-1","event_id":"event-1","type":"task.created","run_id":"run-1","event_sequence":1,"payload":{},"created_at":"2026-07-14T00:00:00Z"},"trace_id":"trace-1"}}`), nil
		}),
	})

	fb := newFakeBus()
	handler := NewTaskHandler(fb, fb, cpClient)
	req := httptest.NewRequest(
		http.MethodPost, "/api/tasks",
		strings.NewReader(`{"user_goal":"hello","conversation_id":"conv-existing","workspace_path":"/workspace"}`),
	)
	rec := httptest.NewRecorder()
	handler.CreateTask(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("unexpected status %d: %s", rec.Code, rec.Body.String())
	}
	var result struct {
		OK   bool                       `json:"ok"`
		Data contracts.CreateTaskOutput `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if result.Data.Conversation.ID != "conv-existing" || result.Data.Task.ConversationID != "conv-existing" {
		t.Fatalf("conversation contract not preserved: %#v", result.Data)
	}
	if result.Data.Task.WorkspacePath != "/workspace" {
		t.Fatalf("workspace contract not preserved: %#v", result.Data.Task)
	}
}

func TestCreateTaskPreservesWorkspacePolicyError(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return jsonResponse(`{"ok":false,"error":{"code":"WORKSPACE_ACCESS_DENIED","message":"所选工作区不在服务端允许范围内","category":"permission","recoverable":false}}`), nil
		}),
	})
	fb := newFakeBus()
	handler := NewTaskHandler(fb, fb, cpClient)
	req := httptest.NewRequest(
		http.MethodPost,
		"/api/tasks",
		strings.NewReader(`{"user_goal":"read","workspace_path":"/outside"}`),
	)
	rec := httptest.NewRecorder()

	handler.CreateTask(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("workspace permission error status=%d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "WORKSPACE_ACCESS_DENIED") ||
		!strings.Contains(rec.Body.String(), "所选工作区不在服务端允许范围内") {
		t.Fatalf("structured workspace error lost: %s", rec.Body.String())
	}
}

func TestListPendingPermissionsMapsControlPlaneDTO(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodGet || r.URL.Path != "/internal/runs/run-1/permissions" {
				t.Fatalf("unexpected permission request: %s %s", r.Method, r.URL.Path)
			}
			return jsonResponse(`{"ok":true,"data":{"requests":[{"id":"req-1","task_id":"task-1","run_id":"run-1","step_id":"step-1","tool_name":"workspace.write_file","action_summary":"写入 notes.md","risk_level":"L2","scope":{"type":"once","workspace_path":"/workspace","path":"notes.md"},"arguments_summary":{"path":"notes.md"},"allowed_decisions":["allow_once","deny"],"created_at":"2026-07-16T00:00:00Z","status":"pending"}]}}`), nil
		}),
	})
	fb := newFakeBus()
	h := NewRunHandler(fb, fb, cpClient)
	w := httptest.NewRecorder()
	h.ListPendingPermissions(
		w,
		httptest.NewRequest(http.MethodGet, "/api/runs/run-1/permissions", nil),
		"run-1",
	)

	if w.Code != http.StatusOK {
		t.Fatalf("unexpected status %d: %s", w.Code, w.Body.String())
	}
	var resp contracts.ApiResult[struct {
		Requests []contracts.PermissionRequestDTO `json:"requests"`
	}]
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Data == nil || len(resp.Data.Requests) != 1 {
		t.Fatalf("pending permissions missing: %s", w.Body.String())
	}
	request := resp.Data.Requests[0]
	if request.ID != "req-1" || request.RunID != "run-1" || request.Scope.Path != "notes.md" {
		t.Fatalf("permission DTO mapping lost: %#v", request)
	}
}

func TestResolvePermissionReturnsImmediateAcknowledgementEvent(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodPost || r.URL.Path != "/internal/permissions/decide" {
				t.Fatalf("unexpected permission request: %s %s", r.Method, r.URL.Path)
			}
			return jsonResponse(`{"ok":true,"data":{"request":{"id":"req-1","task_id":"task-1","run_id":"run-1","step_id":"step-1","tool_name":"rag.ingest_artifact","action_summary":"加入 RAG","risk_level":"L2","scope":{"type":"once","tool_name":"rag.ingest_artifact"},"arguments_summary":{"artifact_id":"artifact-1"},"allowed_decisions":["allow_once","deny"],"created_at":"2026-07-30T00:00:00Z","status":"approved","decision":"allow_once"},"events":[]}}`), nil
		}),
	})
	fb := newFakeBus()
	h := NewRunHandler(fb, fb, cpClient)
	w := httptest.NewRecorder()
	h.ResolvePermission(
		w,
		httptest.NewRequest(
			http.MethodPost,
			"/api/permissions/resolve",
			strings.NewReader(`{"request_id":"req-1","decision":"allow_once"}`),
		),
	)

	if w.Code != http.StatusOK {
		t.Fatalf("unexpected status %d: %s", w.Code, w.Body.String())
	}
	var resp contracts.ApiResult[contracts.ResolvePermissionOutput]
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Data == nil || len(resp.Data.Events) != 1 {
		t.Fatalf("permission acknowledgement missing: %s", w.Body.String())
	}
	event := resp.Data.Events[0]
	if event.Type != "permission.resolved" || event.RunID != "run-1" ||
		event.Payload["request_id"] != "req-1" || event.Payload["decision"] != "allow_once" {
		t.Fatalf("unexpected acknowledgement event: %#v", event)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func jsonResponse(body string) *http.Response {
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(bytes.NewBufferString(body)),
	}
}

// ── Conversation pagination tests ──

func TestGetConversationPassesLimitToControlPlane(t *testing.T) {
	const cursor = "test-cursor+/=_-"
	var capturedLimit string
	var capturedBefore string
	cpClient := controlplane.NewClientWithHTTPClient("http://cp", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			capturedLimit = r.URL.Query().Get("limit")
			capturedBefore = r.URL.Query().Get("before")
			body := `{"ok":true,"data":{"conversation":{"id":"c1","title":"t","created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"},"messages":[],"next_cursor":null}}`
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{"Content-Type": []string{"application/json"}}}, nil
		}),
	})
	h := NewTaskHandler(nil, nil, cpClient)

	req := httptest.NewRequest(http.MethodGet, "/api/conversations/c1?limit=30&before="+url.QueryEscape(cursor), nil)
	w := httptest.NewRecorder()
	h.GetConversation(w, req, "c1")

	if w.Code != 200 {
		t.Fatalf("expected 200, got %d: %s", w.Code, w.Body.String())
	}
	if capturedLimit != "30" {
		t.Errorf("limit should remain 30, got %q", capturedLimit)
	}
	if capturedBefore != cursor {
		t.Errorf("before cursor changed: want %q, got %q", cursor, capturedBefore)
	}
}

func TestGetConversationInvalidLimit(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://cp", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{}`)), Header: http.Header{"Content-Type": []string{"application/json"}}}, nil
		}),
	})
	h := NewTaskHandler(nil, nil, cpClient)

	for _, tc := range []struct{ name, limit string }{
		{"zero", "0"}, {"negative", "-1"}, {"over_max", "101"}, {"non_numeric", "abc"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/api/conversations/c1?limit="+tc.limit, nil)
			w := httptest.NewRecorder()
			h.GetConversation(w, req, "c1")
			if w.Code != 400 {
				t.Errorf("expected 400, got %d", w.Code)
			}
			var resp contracts.ApiResult[interface{}]
			json.Unmarshal(w.Body.Bytes(), &resp)
			if resp.Error == nil || resp.Error.Category != "validation" {
				t.Errorf("expected validation error")
			}
		})
	}
}

func TestGetConversationMapsControlPlaneErrors(t *testing.T) {
	for _, tc := range []struct {
		name     string
		cpErr    *controlplane.ControlPlaneError
		wantCode int
		wantCat  string
	}{
		{"validation", &controlplane.ControlPlaneError{Code: "BAD_CURSOR", Message: "invalid", Category: "validation", Recoverable: true}, 400, "validation"},
		{"not_found", &controlplane.ControlPlaneError{Code: "NOT_FOUND", Message: "not found", Category: "not_found", Recoverable: false}, 404, "not_found"},
		{"permission", &controlplane.ControlPlaneError{Code: "FORBIDDEN", Message: "forbidden", Category: "permission", Recoverable: false}, 403, "permission"},
		{"storage", &controlplane.ControlPlaneError{Code: "DB_DOWN", Message: "db down", Category: "storage", Recoverable: false}, 503, "storage"},
		{"unknown", &controlplane.ControlPlaneError{Code: "PANIC", Message: "panic", Category: "internal", Recoverable: false}, 500, "internal"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body, err := json.Marshal(map[string]interface{}{
				"ok": false,
				"error": map[string]interface{}{
					"code": tc.cpErr.Code, "message": tc.cpErr.Message,
					"category": tc.cpErr.Category, "recoverable": tc.cpErr.Recoverable,
				},
			})
			if err != nil {
				t.Fatal(err)
			}
			cpClient := controlplane.NewClientWithHTTPClient("http://cp", &http.Client{
				Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
					return &http.Response{
						StatusCode: tc.wantCode,
						Body:       io.NopCloser(bytes.NewReader(body)),
						Header:     http.Header{"Content-Type": []string{"application/json"}},
					}, nil
				}),
			})
			h := NewTaskHandler(nil, nil, cpClient)
			w := httptest.NewRecorder()
			h.GetConversation(w, httptest.NewRequest(http.MethodGet, "/api/conversations/c1", nil), "c1")

			if w.Code != tc.wantCode {
				t.Fatalf("status: want %d, got %d: %s", tc.wantCode, w.Code, w.Body.String())
			}
			var resp contracts.ApiResult[interface{}]
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatal(err)
			}
			if resp.Error == nil || resp.Error.Code != tc.cpErr.Code || resp.Error.Message != tc.cpErr.Message || resp.Error.Category != tc.wantCat || resp.Error.Recoverable != tc.cpErr.Recoverable {
				t.Fatalf("AppError not preserved: %#v", resp.Error)
			}
		})
	}
}

func TestConversationHandlersRejectNonGetMethods(t *testing.T) {
	h := NewTaskHandler(nil, nil, nil)
	for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete} {
		for _, endpoint := range []string{"list", "detail"} {
			t.Run(method+"_"+endpoint, func(t *testing.T) {
				w := httptest.NewRecorder()
				req := httptest.NewRequest(method, "/api/conversations/c1", nil)
				if endpoint == "list" {
					h.ListConversations(w, req)
				} else {
					h.GetConversation(w, req, "c1")
				}
				if w.Code != http.StatusMethodNotAllowed {
					t.Fatalf("want 405, got %d", w.Code)
				}
				if w.Header().Get("Allow") != http.MethodGet {
					t.Fatalf("Allow header: want GET, got %q", w.Header().Get("Allow"))
				}
				var resp contracts.ApiResult[interface{}]
				if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
					t.Fatal(err)
				}
				if resp.Error == nil || resp.Error.Code != "METHOD_NOT_ALLOWED" || resp.Error.Category != "validation" {
					t.Fatalf("unexpected AppError: %#v", resp.Error)
				}
			})
		}
	}
}

func TestGetConversationRejectsMissingID(t *testing.T) {
	h := NewTaskHandler(nil, nil, nil)
	w := httptest.NewRecorder()
	h.GetConversation(w, httptest.NewRequest(http.MethodGet, "/api/conversations/", nil), "")
	if w.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", w.Code)
	}
	var resp contracts.ApiResult[interface{}]
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if resp.Error == nil || resp.Error.Code != "VALIDATION_ERROR" || resp.Error.Category != "validation" {
		t.Fatalf("unexpected AppError: %#v", resp.Error)
	}
}

func TestGetConversationPropagatesNextCursor(t *testing.T) {
	var capturedLimit string
	body := `{"ok":true,"data":{"conversation":{"id":"c1","title":"t","created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"},"messages":[],"next_cursor":"cursor-abc"}}`
	cpClient := controlplane.NewClientWithHTTPClient("http://cp", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			capturedLimit = r.URL.Query().Get("limit")
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body)), Header: http.Header{"Content-Type": []string{"application/json"}}}, nil
		}),
	})
	h := NewTaskHandler(nil, nil, cpClient)

	req := httptest.NewRequest(http.MethodGet, "/api/conversations/c1", nil)
	w := httptest.NewRecorder()
	h.GetConversation(w, req, "c1")

	var resp contracts.ApiResult[contracts.ConversationDetailOutput]
	json.Unmarshal(w.Body.Bytes(), &resp)
	if !resp.Ok || resp.Data == nil || resp.Data.NextCursor == nil || *resp.Data.NextCursor != "cursor-abc" {
		t.Errorf("expected next_cursor=cursor-abc, got %v", resp.Data)
	}
	if capturedLimit != "50" {
		t.Errorf("default limit: want 50, got %q", capturedLimit)
	}
}
