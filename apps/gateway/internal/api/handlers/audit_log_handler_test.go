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

func TestAuditLogHandlerMapsSafeAuthorityProjection(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodGet || r.URL.Path != "/internal/audit-logs" || r.URL.Query().Get("event_type") != "model.test" {
				t.Fatalf("unexpected request: %s", r.URL.String())
			}
			return jsonResponse(`{"ok":true,"data":{"audit_logs":[{"id":"a1","event_type":"model.test","actor":"system","action_summary":"模型连通性测试","details_summary":{"api_key":"[已脱敏]"},"created_at":"2026-07-20T12:00:00Z"}],"next_cursor":"cursor"}}`), nil
		}),
	})
	h := NewAuditLogHandler(cpClient)
	rec := httptest.NewRecorder()
	h.ListAuditLogs(rec, httptest.NewRequest(http.MethodGet, "/api/audit-logs?event_type=model.test", nil))
	var response contracts.ApiResult[contracts.ListAuditLogsOutput]
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if rec.Code != http.StatusOK || response.Data == nil || len(response.Data.AuditLogs) != 1 {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}
	if response.Data.AuditLogs[0].DetailsSummary["api_key"] != "[已脱敏]" || response.Data.NextCursor == nil {
		t.Fatalf("safe projection lost: %#v", response.Data)
	}
}

func TestAuditLogHandlerRejectsInvalidFiltersBeforeControlPlane(t *testing.T) {
	h := NewAuditLogHandler(nil)
	rec := httptest.NewRecorder()
	h.ListAuditLogs(rec, httptest.NewRequest(http.MethodGet, "/api/audit-logs?limit=101", nil))
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "VALIDATION_ERROR") {
		t.Fatalf("invalid limit was not rejected: %d %s", rec.Code, rec.Body.String())
	}
}

func TestAuditLogHandlerStreamsBoundedExportWithoutApiEnvelope(t *testing.T) {
	const exportBody = `{"id":"a1","event_type":"model.test"}` + "\n"
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			q := r.URL.Query()
			if r.Method != http.MethodGet || r.URL.Path != "/internal/audit-logs/export" {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.String())
			}
			if q.Get("format") != "jsonl" || q.Get("max_rows") != "25" || q.Get("max_bytes") != "1024" {
				t.Fatalf("export bounds lost: %s", r.URL.RawQuery)
			}
			return &http.Response{
				StatusCode: http.StatusOK,
				Body:       io.NopCloser(strings.NewReader(exportBody)),
				Header:     http.Header{"Content-Type": []string{"application/x-ndjson"}},
			}, nil
		}),
	})
	h := NewAuditLogHandler(cpClient)
	rec := httptest.NewRecorder()
	h.ExportAuditLogs(
		rec,
		httptest.NewRequest(
			http.MethodGet,
			"/api/audit-logs/export?format=jsonl&max_rows=25&max_bytes=1024",
			nil,
		),
	)

	if rec.Code != http.StatusOK || rec.Body.String() != exportBody {
		t.Fatalf("unexpected export: %d %q", rec.Code, rec.Body.String())
	}
	if got := rec.Header().Get("Content-Disposition"); got != `attachment; filename="jarvis-audit-export.jsonl"` {
		t.Fatalf("unexpected disposition: %q", got)
	}
	if rec.Header().Get("Cache-Control") != "no-store" || rec.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("security headers missing: %#v", rec.Header())
	}
}

func TestAuditLogHandlerRejectsInvalidExportBudgetBeforeControlPlane(t *testing.T) {
	h := NewAuditLogHandler(nil)
	rec := httptest.NewRecorder()
	h.ExportAuditLogs(
		rec,
		httptest.NewRequest(
			http.MethodGet,
			"/api/audit-logs/export?format=xml&max_bytes=10",
			nil,
		),
	)
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "VALIDATION_ERROR") {
		t.Fatalf("invalid export was not rejected: %d %s", rec.Code, rec.Body.String())
	}
}

func TestAuditLogHandlerMapsControlPlaneExportError(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			return &http.Response{
				StatusCode: http.StatusBadRequest,
				Body: io.NopCloser(strings.NewReader(
					`{"ok":false,"error":{"code":"VALIDATION_ERROR","message":"无效 cursor","category":"validation","recoverable":false}}`,
				)),
				Header: http.Header{"Content-Type": []string{"application/json"}},
			}, nil
		}),
	})
	h := NewAuditLogHandler(cpClient)
	rec := httptest.NewRecorder()
	h.ExportAuditLogs(
		rec,
		httptest.NewRequest(http.MethodGet, "/api/audit-logs/export", nil),
	)
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "VALIDATION_ERROR") {
		t.Fatalf("control plane error not mapped: %d %s", rec.Code, rec.Body.String())
	}
}

func TestAuditLogHandlerMapsRetentionPreview(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			q := r.URL.Query()
			if r.URL.Path != "/internal/audit-logs/retention/preview" ||
				q.Get("standard_days") != "90" ||
				q.Get("extended_days") != "365" ||
				q.Get("max_scan") != "500" ||
				q.Get("max_candidates") != "50" {
				t.Fatalf("unexpected preview request: %s", r.URL.String())
			}
			return jsonResponse(`{"ok":true,"data":{"dry_run":true,"standard_days":90,"extended_days":365,"standard_before":"2026-05-02T00:00:00Z","extended_before":"2025-07-31T00:00:00Z","max_scan":500,"max_candidates":50,"scanned_records":12,"candidate_records":3,"protected_records":4,"extended_retained_records":5,"has_more":false}}`), nil
		}),
	})
	h := NewAuditLogHandler(cpClient)
	rec := httptest.NewRecorder()
	h.PreviewAuditRetention(
		rec,
		httptest.NewRequest(
			http.MethodGet,
			"/api/audit-logs/retention/preview?max_scan=500&max_candidates=50",
			nil,
		),
	)
	var response contracts.ApiResult[contracts.AuditRetentionPreviewDTO]
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if rec.Code != http.StatusOK || response.Data == nil {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}
	if !response.Data.DryRun || response.Data.CandidateRecords != 3 ||
		response.Data.ProtectedRecords != 4 || response.Data.HasMore {
		t.Fatalf("preview projection lost: %#v", response.Data)
	}
}

func TestAuditLogHandlerRejectsUnsafeRetentionPolicyBeforeControlPlane(t *testing.T) {
	h := NewAuditLogHandler(nil)
	rec := httptest.NewRecorder()
	h.PreviewAuditRetention(
		rec,
		httptest.NewRequest(
			http.MethodGet,
			"/api/audit-logs/retention/preview?standard_days=365&extended_days=90",
			nil,
		),
	)
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "VALIDATION_ERROR") {
		t.Fatalf("unsafe retention policy was not rejected: %d %s", rec.Code, rec.Body.String())
	}
}

func TestAuditLogHandlerCreatesL4RetentionRequestWithoutDeleting(t *testing.T) {
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodPost || r.URL.Path != "/internal/audit-logs/retention/requests" {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.String())
			}
			body, err := io.ReadAll(r.Body)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.Contains(string(body), `"max_candidates":25`) {
				t.Fatalf("retention bounds lost: %s", body)
			}
			return jsonResponse(`{"ok":true,"data":{"request":{"id":"123e4567-e89b-12d3-a456-426614174000","task_id":"123e4567-e89b-12d3-a456-426614174001","run_id":"123e4567-e89b-12d3-a456-426614174002","tool_name":"audit.apply_retention_policy","action_summary":"永久删除过期审计记录","reason":"不可撤销","risk_level":"L4","scope":{"type":"once","resource":"audit_logs"},"arguments_summary":{"candidate_records":3},"allowed_decisions":["allow_once","deny"],"created_at":"2026-07-31T00:00:00Z","status":"pending"}}}`), nil
		}),
	})
	rec := httptest.NewRecorder()
	body := strings.NewReader(`{"standard_days":90,"extended_days":365,"max_scan":500,"max_candidates":25}`)
	NewAuditLogHandler(cpClient).CreateAuditRetentionRequest(
		rec,
		httptest.NewRequest(http.MethodPost, "/api/audit-logs/retention/requests", body),
	)

	var response contracts.ApiResult[contracts.CreateAuditRetentionRequestOutput]
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if rec.Code != http.StatusOK || response.Data == nil ||
		response.Data.Request.RiskLevel != "L4" ||
		response.Data.Request.Scope.Resource != "audit_logs" {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}
}

func TestAuditLogHandlerResolvesRetentionOnlyWithOnceOrDeny(t *testing.T) {
	const requestID = "123e4567-e89b-12d3-a456-426614174000"
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			expected := "/internal/audit-logs/retention/requests/" + requestID + "/resolve"
			if r.Method != http.MethodPost || r.URL.Path != expected {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.String())
			}
			return jsonResponse(`{"ok":true,"data":{"permission":{"id":"` + requestID + `","task_id":"123e4567-e89b-12d3-a456-426614174001","run_id":"123e4567-e89b-12d3-a456-426614174002","tool_name":"audit.apply_retention_policy","action_summary":"永久删除过期审计记录","risk_level":"L4","scope":{"type":"once","resource":"audit_logs"},"arguments_summary":{},"allowed_decisions":["allow_once","deny"],"created_at":"2026-07-31T00:00:00Z","status":"consumed","decision":"allow_once"},"deleted_records":3,"has_more":true}}`), nil
		}),
	})
	h := NewAuditLogHandler(cpClient)
	rec := httptest.NewRecorder()
	h.ResolveAuditRetentionRequest(
		rec,
		httptest.NewRequest(http.MethodPost, "/api/audit-logs/retention/requests/"+requestID+"/resolve", strings.NewReader(`{"decision":"allow_once"}`)),
		requestID,
	)
	var response contracts.ApiResult[contracts.AuditRetentionResolutionDTO]
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if rec.Code != http.StatusOK || response.Data == nil ||
		response.Data.DeletedRecords != 3 || !response.Data.HasMore {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}

	invalid := httptest.NewRecorder()
	h.ResolveAuditRetentionRequest(
		invalid,
		httptest.NewRequest(http.MethodPost, "/api/audit-logs/retention/requests/"+requestID+"/resolve", strings.NewReader(`{"decision":"always_allow_for_workspace"}`)),
		requestID,
	)
	if invalid.Code != http.StatusBadRequest {
		t.Fatalf("permanent grant was not rejected: %d %s", invalid.Code, invalid.Body.String())
	}
}
