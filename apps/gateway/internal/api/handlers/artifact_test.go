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

func TestArtifactHandlerReturnsContentWithoutFilePath(t *testing.T) {
	id := "11111111-1111-4111-8111-111111111111"
	cpClient := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodGet || r.URL.Path != "/internal/artifacts/"+id {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
			}
			return jsonResponse(`{"ok":true,"data":{"artifact":{"id":"` + id + `","task_id":"t1","run_id":"r1","kind":"markdown","title":"最终回复","purpose":"final_response","producer":{"type":"runtime"},"content":"正文","file_size_bytes":6,"mime_type":"text/markdown; charset=utf-8","content_hash":"abc","metadata":{"storage":"local_file"},"created_at":"2026-07-23T00:00:00Z"}}}`), nil
		}),
	})
	handler := NewArtifactHandler(cpClient)
	rec := httptest.NewRecorder()
	handler.GetArtifact(rec, httptest.NewRequest(http.MethodGet, "/api/artifacts/"+id, nil), id)

	var response contracts.ApiResult[contracts.ArtifactDTO]
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if rec.Code != http.StatusOK || response.Data == nil || response.Data.Content != "正文" {
		t.Fatalf("unexpected response: %d %s", rec.Code, rec.Body.String())
	}
	if response.Data.Purpose != "final_response" || response.Data.Producer.Type != "runtime" {
		t.Fatalf("artifact v2 contract lost: %s", rec.Body.String())
	}
	if response.Data.FilePath != "" || strings.Contains(rec.Body.String(), "file_path") {
		t.Fatalf("local file reference leaked: %s", rec.Body.String())
	}
}

func TestArtifactHandlerRejectsInvalidID(t *testing.T) {
	rec := httptest.NewRecorder()
	NewArtifactHandler(nil).GetArtifact(
		rec, httptest.NewRequest(http.MethodGet, "/api/artifacts/bad", nil), "bad",
	)
	if rec.Code != http.StatusBadRequest || !strings.Contains(rec.Body.String(), "VALIDATION_ERROR") {
		t.Fatalf("invalid id was not rejected: %d %s", rec.Code, rec.Body.String())
	}
}
