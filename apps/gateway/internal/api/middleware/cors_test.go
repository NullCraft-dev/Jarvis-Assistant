package middleware

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCORSAllowsConfiguredOrigin(t *testing.T) {
	t.Setenv("JARVIS_CORS_ORIGINS", "http://localhost:5173")
	called := false
	handler := CORS(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodGet, "/api/workspaces", nil)
	req.Header.Set("Origin", "http://localhost:5173")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if !called || rec.Code != http.StatusNoContent {
		t.Fatalf("allowed origin did not reach handler: called=%v status=%d", called, rec.Code)
	}
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "http://localhost:5173" {
		t.Fatalf("unexpected allow origin: %q", got)
	}
}

func TestCORSRejectsUnknownOriginBeforeHandler(t *testing.T) {
	t.Setenv("JARVIS_CORS_ORIGINS", "http://localhost:5173")
	called := false
	handler := CORS(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusNoContent)
	}))

	req := httptest.NewRequest(http.MethodPost, "/api/workspaces/pick", strings.NewReader("ignored"))
	req.Header.Set("Origin", "https://evil.example")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if called {
		t.Fatal("unknown origin reached state-changing handler")
	}
	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "ORIGIN_NOT_ALLOWED") {
		t.Fatalf("unexpected rejection: status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestCORSHandlesAllowedPreflight(t *testing.T) {
	t.Setenv("JARVIS_CORS_ORIGINS", "http://127.0.0.1:5173")
	handler := CORS(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("preflight should not reach handler")
	}))

	req := httptest.NewRequest(http.MethodOptions, "/api/workspaces/pick", nil)
	req.Header.Set("Origin", "http://127.0.0.1:5173")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusNoContent {
		t.Fatalf("preflight status=%d", rec.Code)
	}
	if got := rec.Header().Get("Access-Control-Allow-Methods"); !strings.Contains(got, http.MethodPost) || !strings.Contains(got, http.MethodPatch) {
		t.Fatalf("preflight methods must include POST and PATCH: %q", got)
	}
}

func TestCORSAllowsRequestsWithoutOrigin(t *testing.T) {
	called := false
	handler := CORS(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		called = true
		w.WriteHeader(http.StatusNoContent)
	}))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/health", nil))
	if !called || rec.Code != http.StatusNoContent {
		t.Fatalf("origin-less local request rejected: called=%v status=%d", called, rec.Code)
	}
}
