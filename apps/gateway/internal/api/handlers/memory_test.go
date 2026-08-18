package handlers

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

func TestMemoryHandlerCreatesTypedMemory(t *testing.T) {
	id := "11111111-1111-4111-8111-111111111111"
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodPost || r.URL.Path != "/internal/memories" {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
			}
			body, _ := io.ReadAll(r.Body)
			if !strings.Contains(string(body), `"scope_type":"global"`) {
				t.Fatalf("scope contract lost: %s", body)
			}
			return jsonResponse(`{"ok":true,"data":{"memory":{"id":"` + id + `","scope_type":"global","category":"preference","key":"response.language","content":"中文","status":"active","source_type":"user_explicit","importance":60,"version":1,"created_at":"2026-07-24T00:00:00Z","updated_at":"2026-07-24T00:00:00Z"}}}`), nil
		}),
	})
	handler := NewMemoryHandler(client)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/memories", strings.NewReader(`{"scope_type":"global","category":"preference","key":"response.language","content":"中文","importance":60}`))
	handler.Collection(rec, req)
	var response contracts.ApiResult[contracts.MemoryMutationOutput]
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 || response.Data == nil || response.Data.Memory.SourceType != "user_explicit" {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}
}

func TestMemoryHandlerRejectsInvalidID(t *testing.T) {
	rec := httptest.NewRecorder()
	NewMemoryHandler(nil).Item(rec, httptest.NewRequest(http.MethodDelete, "/api/memories/bad", nil), "bad")
	if rec.Code != 400 || !strings.Contains(rec.Body.String(), "VALIDATION_ERROR") {
		t.Fatalf("invalid id was not rejected: %d %s", rec.Code, rec.Body.String())
	}
}

func TestMemoryCandidateHandlerApprovesThroughTypedBoundary(t *testing.T) {
	id := "22222222-2222-4222-8222-222222222222"
	memoryID := "33333333-3333-4333-8333-333333333333"
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodPost || r.URL.Path != "/internal/memory-candidates/"+id+"/approve" {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
			}
			body, _ := io.ReadAll(r.Body)
			if !strings.Contains(string(body), `"expected_version":1`) {
				t.Fatalf("candidate decision contract lost: %s", body)
			}
			return jsonResponse(`{"ok":true,"data":{"candidate":{"id":"` + id + `","scope_type":"global","category":"preference","suggested_key":"response.language","content":"中文","status":"approved","source_task_id":"44444444-4444-4444-8444-444444444444","source_run_id":"55555555-5555-4555-8555-555555555555","confidence":0.9,"importance":80,"sensitivity":"normal","approved_memory_id":"` + memoryID + `","extraction_policy_version":"memory-extraction-v1","version":2,"created_at":"2026-07-26T00:00:00Z","updated_at":"2026-07-26T00:01:00Z"},"memory":{"id":"` + memoryID + `","scope_type":"global","category":"preference","key":"response.language","content":"中文","status":"active","source_type":"candidate_approved","importance":80,"version":1,"created_at":"2026-07-26T00:01:00Z","updated_at":"2026-07-26T00:01:00Z"}}}`), nil
		}),
	})
	handler := NewMemoryHandler(client)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/memory-candidates/"+id+"/approve", strings.NewReader(`{"expected_version":1}`))
	handler.CandidateItem(rec, req, id, "approve")
	var response contracts.ApiResult[contracts.ApproveMemoryCandidateOutput]
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 || response.Data == nil || response.Data.Memory.SourceType != "candidate_approved" {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}
}
