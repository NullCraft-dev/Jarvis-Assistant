package app

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/orchestrator"
)

func newRouterForContractTest() http.Handler {
	bus := orchestrator.NewInMemoryRuntimeBus()
	return buildRouter(orchestrator.RuntimeBusConfig{BusType: "inmemory"}, bus, bus, nil)
}

func TestRouterWritesUniformErrorsForRouteValidation(t *testing.T) {
	router := newRouterForContractTest()
	for _, request := range []*http.Request{
		httptest.NewRequest(http.MethodDelete, "/api/tasks", nil),
		httptest.NewRequest(http.MethodGet, "/api/tasks/", nil),
		httptest.NewRequest(http.MethodGet, "/api/rag/documents/not-an-id/unexpected", nil),
	} {
		rec := httptest.NewRecorder()
		router.ServeHTTP(rec, request)
		if !strings.Contains(rec.Header().Get("Content-Type"), "application/json") {
			t.Fatalf("%s %s must return JSON: %q", request.Method, request.URL.Path, rec.Header().Get("Content-Type"))
		}
		body := rec.Body.String()
		if !strings.Contains(body, `"ok":false`) || !strings.Contains(body, `"error"`) {
			t.Fatalf("%s %s must return ApiResult error: %s", request.Method, request.URL.Path, body)
		}
	}
}
