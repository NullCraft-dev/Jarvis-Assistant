package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

func TestListWorkspacesMapsAuthorityResponse(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodGet || r.URL.Path != "/internal/workspaces" {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
			}
			return jsonResponse(`{"ok":true,"data":{"workspaces":[{"id":"ws-1","name":"jarvis","root_path":"/project","canonical_path":"/project","status":"active","source":"configured","created_at":"2026-07-16T00:00:00Z","updated_at":"2026-07-16T00:00:00Z"}]}}`), nil
		}),
	})
	handler := NewWorkspaceHandler(cpClient)
	rec := httptest.NewRecorder()
	handler.ListWorkspaces(rec, httptest.NewRequest(http.MethodGet, "/api/workspaces", nil))

	var response contracts.ApiResult[contracts.ListWorkspacesOutput]
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if rec.Code != http.StatusOK || response.Data == nil || len(response.Data.Workspaces) != 1 {
		t.Fatalf("unexpected response: status=%d body=%s", rec.Code, rec.Body.String())
	}
	if response.Data.Workspaces[0].ID != "ws-1" || response.Data.Workspaces[0].Source != "configured" {
		t.Fatalf("workspace mapping lost: %#v", response.Data.Workspaces[0])
	}
}

func TestPickWorkspacePreservesCancellation(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodPost || r.URL.Path != "/internal/workspaces/pick" {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
			}
			return jsonResponse(`{"ok":true,"data":{"workspace":null,"cancelled":true}}`), nil
		}),
	})
	handler := NewWorkspaceHandler(cpClient)
	rec := httptest.NewRecorder()
	handler.PickWorkspace(rec, httptest.NewRequest(http.MethodPost, "/api/workspaces/pick", nil))

	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"cancelled":true`) {
		t.Fatalf("picker cancellation lost: status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestRevokeConfiguredWorkspacePreservesStructuredError(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return jsonResponse(`{"ok":false,"error":{"code":"WORKSPACE_MANAGED_BY_CONFIG","message":"该工作区由服务端配置管理，无法通过 Web 撤销","category":"permission","recoverable":false}}`), nil
		}),
	})
	handler := NewWorkspaceHandler(cpClient)
	rec := httptest.NewRecorder()
	handler.RevokeWorkspace(rec, httptest.NewRequest(http.MethodDelete, "/api/workspaces/ws-1", nil), "ws-1")

	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "WORKSPACE_MANAGED_BY_CONFIG") {
		t.Fatalf("configured revoke error lost: status=%d body=%s", rec.Code, rec.Body.String())
	}
}
