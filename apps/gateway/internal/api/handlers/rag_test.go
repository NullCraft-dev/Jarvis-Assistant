package handlers

import (
	"bytes"
	"encoding/json"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

func TestRagHandlerReturnsDocumentAndLatestJob(t *testing.T) {
	workspaceID := "11111111-1111-4111-8111-111111111111"
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodGet || r.URL.Path != "/internal/rag/documents" || r.URL.Query().Get("workspace_id") != workspaceID {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.String())
			}
			return jsonResponse(`{"ok":true,"data":{"documents":[{"id":"22222222-2222-4222-8222-222222222222","workspace_id":"` + workspaceID + `","source_artifact_id":"33333333-3333-4333-8333-333333333333","title":"paper.pdf","mime_type":"application/pdf","status":"ready","ingestion_policy_version":"rag-v1","parser_version":"pymupdf-v1","chunker_version":"structure-v1","embedding_provider":"openai","embedding_model":"text-embedding-3-small","embedding_dimensions":1536,"chunk_count":48,"version":2,"created_at":"2026-07-28T00:00:00Z","updated_at":"2026-07-28T00:01:00Z","latest_job":{"id":"44444444-4444-4444-8444-444444444444","status":"completed","attempts":1,"max_attempts":3,"embedding_attempts":1,"embedding_max_attempts":3,"progress":{"page_count":316,"visual_pages_total":108,"visual_route_counts":{"ocr_required":4,"complex_image":103,"complex_table":1}},"created_at":"2026-07-28T00:00:00Z","updated_at":"2026-07-28T00:01:00Z"}}]}}`), nil
		}),
	})
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/rag/documents?workspace_id="+workspaceID, nil)
	NewRagHandler(client).Documents(recorder, request)

	var response contracts.ApiResult[contracts.ListRagDocumentsOutput]
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if recorder.Code != http.StatusOK || response.Data == nil || len(response.Data.Documents) != 1 {
		t.Fatalf("unexpected response: %d %s", recorder.Code, recorder.Body.String())
	}
	document := response.Data.Documents[0]
	if document.ChunkCount != 48 || document.LatestJob == nil || document.LatestJob.Status != "completed" {
		t.Fatalf("RAG contract lost: %s", recorder.Body.String())
	}
	if document.LatestJob.Progress.VisualRouteCounts["complex_image"] != 103 {
		t.Fatalf("RAG visual route counts lost: %s", recorder.Body.String())
	}
}

func TestRagHandlerRejectsInvalidWorkspaceID(t *testing.T) {
	recorder := httptest.NewRecorder()
	NewRagHandler(nil).Documents(recorder, httptest.NewRequest(http.MethodGet, "/api/rag/documents?workspace_id=bad", nil))
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("invalid workspace id was not rejected: %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestRagFeedbackUsesMessageBoundaryAndSafeReviewProjection(t *testing.T) {
	workspaceID := "11111111-1111-4111-8111-111111111111"
	messageID := "22222222-2222-4222-8222-222222222222"
	feedbackID := "33333333-3333-4333-8333-333333333333"
	requests := 0
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			requests++
			if requests == 1 {
				body, _ := io.ReadAll(r.Body)
				if r.Method != http.MethodPost || r.URL.Path != "/internal/rag/feedback" || !bytes.Contains(body, []byte(`"message_id":"`+messageID+`"`)) {
					t.Fatalf("feedback submit contract lost: %s %s", r.URL.Path, body)
				}
				return jsonResponse(`{"ok":true,"data":{"feedback":{"id":"` + feedbackID + `","trace_id":"44444444-4444-4444-8444-444444444444","workspace_id":"` + workspaceID + `","task_id":"55555555-5555-4555-8555-555555555555","run_id":"66666666-6666-4666-8666-666666666666","message_id":"` + messageID + `","kind":"unhelpful","status":"pending","created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:00:00Z"}}}`), nil
			}
			if r.Method != http.MethodGet || r.URL.Path != "/internal/rag/feedback" || r.URL.Query().Get("workspace_id") != workspaceID {
				t.Fatalf("feedback list contract lost: %s %s", r.Method, r.URL.String())
			}
			return jsonResponse(`{"ok":true,"data":{"feedback":[{"id":"` + feedbackID + `","trace_id":"44444444-4444-4444-8444-444444444444","workspace_id":"` + workspaceID + `","task_id":"55555555-5555-4555-8555-555555555555","run_id":"66666666-6666-4666-8666-666666666666","message_id":"` + messageID + `","kind":"unhelpful","status":"pending","query_hash":"abcdef","result_count":3,"context_truncated":false,"created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:00:00Z"}]}}`), nil
		}),
	})

	submitRecorder := httptest.NewRecorder()
	NewRagHandler(client).Feedback(submitRecorder, httptest.NewRequest(http.MethodPost, "/api/rag/feedback", strings.NewReader(`{"message_id":"`+messageID+`","kind":"unhelpful"}`)))
	if submitRecorder.Code != http.StatusOK || !bytes.Contains(submitRecorder.Body.Bytes(), []byte(`"status":"pending"`)) {
		t.Fatalf("unexpected submit response: %d %s", submitRecorder.Code, submitRecorder.Body.String())
	}
	listRecorder := httptest.NewRecorder()
	NewRagHandler(client).Feedback(listRecorder, httptest.NewRequest(http.MethodGet, "/api/rag/feedback?workspace_id="+workspaceID+"&status=pending&limit=50", nil))
	if listRecorder.Code != http.StatusOK || !bytes.Contains(listRecorder.Body.Bytes(), []byte(`"query_hash":"abcdef"`)) {
		t.Fatalf("unexpected list response: %d %s", listRecorder.Code, listRecorder.Body.String())
	}
}

func TestRagFeedbackRejectsUnscopedCitation(t *testing.T) {
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			t.Fatalf("invalid citation feedback reached control plane")
			return nil, nil
		}),
	})
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/rag/feedback", strings.NewReader(`{"message_id":"22222222-2222-4222-8222-222222222222","kind":"citation_incorrect"}`))
	NewRagHandler(client).Feedback(recorder, request)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("unscoped citation feedback was not rejected: %d", recorder.Code)
	}
}

func TestRagFeedbackDiagnosticDetailAndDraftTriageStayTyped(t *testing.T) {
	feedbackID := "33333333-3333-4333-8333-333333333333"
	chunkID := "77777777-7777-4777-8777-777777777777"
	requests := 0
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requests++
		if requests == 1 {
			if r.Method != http.MethodGet || r.URL.Path != "/internal/rag/feedback/"+feedbackID {
				t.Fatalf("inspect contract lost: %s %s", r.Method, r.URL.Path)
			}
			return jsonResponse(`{"ok":true,"data":{"feedback":{"id":"` + feedbackID + `","trace_id":"44444444-4444-4444-8444-444444444444","workspace_id":"11111111-1111-4111-8111-111111111111","task_id":"55555555-5555-4555-8555-555555555555","run_id":"66666666-6666-4666-8666-666666666666","message_id":"22222222-2222-4222-8222-222222222222","kind":"unhelpful","status":"pending","created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:00:00Z"},"query_hash":"abcdef","query":null,"privacy_status":"pending","pipeline_versions":{},"result_count":1,"context_truncated":false,"evidence":[{"chunk_id":"` + chunkID + `","document_id":"88888888-8888-4888-8888-888888888888","content_hash":"hash","candidate_rank":1,"reranked_rank":null,"in_context":true,"sources":["semantic"],"snippet":null}],"label":null}}`), nil
		}
		body, _ := io.ReadAll(r.Body)
		if r.Method != http.MethodPost || r.URL.Path != "/internal/rag/feedback/"+feedbackID+"/triage" || !bytes.Contains(body, []byte(`"failure_category":"answer_generation"`)) {
			t.Fatalf("triage contract lost: %s %s %s", r.Method, r.URL.Path, body)
		}
		return jsonResponse(`{"ok":true,"data":{"feedback":{"id":"` + feedbackID + `","trace_id":"44444444-4444-4444-8444-444444444444","workspace_id":"11111111-1111-4111-8111-111111111111","task_id":"55555555-5555-4555-8555-555555555555","run_id":"66666666-6666-4666-8666-666666666666","message_id":"22222222-2222-4222-8222-222222222222","kind":"unhelpful","status":"reviewed","failure_category":"answer_generation","created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:01:00Z"},"label_status":"draft"}}`), nil
	})})
	handler := NewRagHandler(client)
	inspectRecorder := httptest.NewRecorder()
	handler.FeedbackItem(inspectRecorder, httptest.NewRequest(http.MethodGet, "/api/rag/feedback/"+feedbackID, nil), feedbackID, "")
	if inspectRecorder.Code != http.StatusOK || !bytes.Contains(inspectRecorder.Body.Bytes(), []byte(`"privacy_status":"pending"`)) || bytes.Contains(inspectRecorder.Body.Bytes(), []byte("private evidence")) {
		t.Fatalf("unsafe inspect response: %d %s", inspectRecorder.Code, inspectRecorder.Body.String())
	}
	triageRecorder := httptest.NewRecorder()
	handler.FeedbackItem(triageRecorder, httptest.NewRequest(http.MethodPost, "/api/rag/feedback/"+feedbackID+"/triage", strings.NewReader(`{"failure_category":"answer_generation","positive_chunk_ids":["`+chunkID+`"],"hard_negative_chunk_ids":[]}`)), feedbackID, "triage")
	if triageRecorder.Code != http.StatusOK || !bytes.Contains(triageRecorder.Body.Bytes(), []byte(`"label_status":"draft"`)) {
		t.Fatalf("unexpected triage response: %d %s", triageRecorder.Code, triageRecorder.Body.String())
	}
}

func TestRagEvaluationReviewLifecycleStaysWorkspaceScopedAndTyped(t *testing.T) {
	workspaceID := "11111111-1111-4111-8111-111111111111"
	traceID := "44444444-4444-4444-8444-444444444444"
	chunkID := "77777777-7777-4777-8777-777777777777"
	requests := 0
	detail := `{"ok":true,"data":{"trace":{"trace_id":"` + traceID + `","workspace_id":"` + workspaceID + `","task_id":"55555555-5555-4555-8555-555555555555","run_id":"66666666-6666-4666-8666-666666666666","query_hash":"abcdef","privacy_status":"approved","label_status":"confirmed","label_source":"human_review","candidate_count":1,"reranked_count":1,"context_chunk_count":1,"context_truncated":false,"pipeline_versions":{},"created_at":"2026-08-02T00:00:00Z"},"query":"safe query","request":{},"evidence":[{"chunk_id":"` + chunkID + `","document_id":"88888888-8888-4888-8888-888888888888","content_hash":"hash","candidate_rank":1,"reranked_rank":1,"in_context":true,"sources":["semantic"],"snippet":"safe snippet"}],"label":{"id":"99999999-9999-4999-8999-999999999999","source":"human_review","status":"confirmed","positive_chunk_ids":["` + chunkID + `"],"hard_negative_chunk_ids":[],"notes":"checked"},"promotion_candidate":null}}`
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		requests++
		var body []byte
		if r.Body != nil {
			body, _ = io.ReadAll(r.Body)
		}
		switch requests {
		case 1:
			if r.Method != http.MethodGet || r.URL.Path != "/internal/rag/evaluation/traces" || r.URL.Query().Get("workspace_id") != workspaceID {
				t.Fatalf("list contract lost: %s", r.URL.String())
			}
			return jsonResponse(`{"ok":true,"data":{"traces":[{"trace_id":"` + traceID + `","workspace_id":"` + workspaceID + `","task_id":"55555555-5555-4555-8555-555555555555","run_id":"66666666-6666-4666-8666-666666666666","query_hash":"abcdef","privacy_status":"pending","candidate_count":1,"reranked_count":1,"context_chunk_count":1,"context_truncated":false,"pipeline_versions":{},"created_at":"2026-08-02T00:00:00Z"}]}}`), nil
		case 2:
			if r.Method != http.MethodGet || r.URL.Query().Get("workspace_id") != workspaceID {
				t.Fatalf("inspect scope lost: %s", r.URL.String())
			}
			return jsonResponse(detail), nil
		case 3:
			if r.URL.Path != "/internal/rag/evaluation/traces/"+traceID+"/privacy" || !bytes.Contains(body, []byte(`"decision":"approved"`)) {
				t.Fatalf("privacy contract lost: %s %s", r.URL.Path, body)
			}
			return jsonResponse(detail), nil
		case 4:
			if r.URL.Path != "/internal/rag/evaluation/traces/"+traceID+"/label" || !bytes.Contains(body, []byte(`"status":"confirmed"`)) || !bytes.Contains(body, []byte(chunkID)) {
				t.Fatalf("label contract lost: %s", body)
			}
			return jsonResponse(detail), nil
		case 5:
			if r.URL.Path != "/internal/rag/evaluation/traces/"+traceID+"/promote" || !bytes.Contains(body, []byte(workspaceID)) {
				t.Fatalf("promotion contract lost: %s %s", r.URL.Path, body)
			}
			return jsonResponse(strings.Replace(detail, `"label_status":"confirmed"`, `"label_status":"promoted"`, 1)), nil
		default:
			t.Fatalf("unexpected request count: %d", requests)
			return nil, nil
		}
	})})
	handler := NewRagHandler(client)
	listRecorder := httptest.NewRecorder()
	handler.EvaluationTraces(listRecorder, httptest.NewRequest(http.MethodGet, "/api/rag/evaluation/traces?workspace_id="+workspaceID+"&privacy_status=all", nil))
	if listRecorder.Code != http.StatusOK || !bytes.Contains(listRecorder.Body.Bytes(), []byte(`"query_hash":"abcdef"`)) {
		t.Fatalf("unexpected list: %d %s", listRecorder.Code, listRecorder.Body.String())
	}

	for _, operation := range []struct{ action, body string }{
		{"", ""},
		{"privacy", `{"workspace_id":"` + workspaceID + `","decision":"approved"}`},
		{"label", `{"workspace_id":"` + workspaceID + `","status":"confirmed","positive_chunk_ids":["` + chunkID + `"],"hard_negative_chunk_ids":[],"notes":"checked"}`},
		{"promote", `{"workspace_id":"` + workspaceID + `"}`},
	} {
		recorder := httptest.NewRecorder()
		method := http.MethodPost
		path := "/api/rag/evaluation/traces/" + traceID
		if operation.action == "" {
			method = http.MethodGet
			path += "?workspace_id=" + workspaceID
		} else {
			path += "/" + operation.action
		}
		handler.EvaluationTraceItem(recorder, httptest.NewRequest(method, path, strings.NewReader(operation.body)), traceID, operation.action)
		if recorder.Code != http.StatusOK || !bytes.Contains(recorder.Body.Bytes(), []byte(`"trace_id":"`+traceID+`"`)) {
			t.Fatalf("unexpected %s response: %d %s", operation.action, recorder.Code, recorder.Body.String())
		}
	}
}

func TestRagQualityGateHistoryIsReadOnlyAndTyped(t *testing.T) {
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet || r.URL.Path != "/internal/rag/evaluation/gates" || r.URL.Query().Get("limit") != "20" {
			t.Fatalf("gate history contract lost: %s %s", r.Method, r.URL.String())
		}
		return jsonResponse(`{"ok":true,"data":{"runs":[{"id":"11111111-1111-4111-8111-111111111111","gate_id":"rag-promoted-release-v1","cohort_id":"rag-promoted-p4-v1","baseline_id":"rag-promoted-p4-v1","revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","status":"passed","sample_count":10,"metrics":{"candidate.recall@5":0.9},"checks":[{"check_id":"minimum_sample_count","passed":true,"actual":10,"required":10}],"generated_at":"2026-08-02T08:00:00+00:00"}],"insights":{"comparison_state":"insufficient_history","compatible_history_count":1,"previous_run_id":null,"metric_trends":[],"alerts":[],"failure_clusters":[{"failure_type":"candidate_evidence_missed","priority":"medium","latest_rate":0.2,"latest_count":2,"previous_rate":null,"rate_delta":null,"occurrence_count":1,"threshold":0.35,"check_passed":true}]}}}`), nil
	})})
	recorder := httptest.NewRecorder()
	NewRagHandler(client).EvaluationGates(recorder, httptest.NewRequest(http.MethodGet, "/api/rag/evaluation/gates?limit=20", nil))
	if recorder.Code != http.StatusOK || !bytes.Contains(recorder.Body.Bytes(), []byte(`"candidate.recall@5":0.9`)) || !bytes.Contains(recorder.Body.Bytes(), []byte(`"failure_type":"candidate_evidence_missed"`)) || bytes.Contains(recorder.Body.Bytes(), []byte("raw_query")) {
		t.Fatalf("unexpected gate projection: %d %s", recorder.Code, recorder.Body.String())
	}

	methodRecorder := httptest.NewRecorder()
	NewRagHandler(client).EvaluationGates(methodRecorder, httptest.NewRequest(http.MethodPost, "/api/rag/evaluation/gates", nil))
	if methodRecorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("quality gate endpoint must remain read-only: %d", methodRecorder.Code)
	}
}

func TestRagQualityFailureTargetsAreReadOnlyAndRedacted(t *testing.T) {
	runID := "11111111-1111-4111-8111-111111111111"
	traceID := "22222222-2222-4222-8222-222222222222"
	workspaceID := "33333333-3333-4333-8333-333333333333"
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet || r.URL.Path != "/internal/rag/evaluation/gates/"+runID+"/failure-targets" || r.URL.Query().Get("failure_type") != "candidate_evidence_missed" || r.URL.Query().Get("limit") != "50" {
			t.Fatalf("failure target contract lost: %s %s", r.Method, r.URL.String())
		}
		return jsonResponse(`{"ok":true,"data":{"targets":[{"candidate_id":"` + strings.Repeat("b", 64) + `","trace_id":"` + traceID + `","workspace_id":"` + workspaceID + `","query_hash":"` + strings.Repeat("a", 64) + `","failure_type":"candidate_evidence_missed","suspected_stage":"candidate_recall","severity":"high","metric_ids":["candidate.recall@5"],"privacy_status":"approved","label_status":"promoted","label_source":"human_review","review_state":"fixed_regression_sample"}]}}`), nil
	})})
	recorder := httptest.NewRecorder()
	NewRagHandler(client).EvaluationGateFailureTargets(recorder, httptest.NewRequest(http.MethodGet, "/api/rag/evaluation/gates/"+runID+"/failure-targets?failure_type=candidate_evidence_missed&limit=50", nil), runID)
	if recorder.Code != http.StatusOK || !bytes.Contains(recorder.Body.Bytes(), []byte(`"trace_id":"`+traceID+`"`)) || !bytes.Contains(recorder.Body.Bytes(), []byte(`"review_state":"fixed_regression_sample"`)) || bytes.Contains(recorder.Body.Bytes(), []byte("raw_query")) {
		t.Fatalf("unexpected failure target projection: %d %s", recorder.Code, recorder.Body.String())
	}
	methodRecorder := httptest.NewRecorder()
	NewRagHandler(client).EvaluationGateFailureTargets(methodRecorder, httptest.NewRequest(http.MethodPost, "/api/rag/evaluation/gates/"+runID+"/failure-targets", nil), runID)
	if methodRecorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("failure target endpoint must remain read-only: %d", methodRecorder.Code)
	}
}

func TestRagQualityIssueUpdateUsesOptimisticVersion(t *testing.T) {
	issueID := "44444444-4444-4444-8444-444444444444"
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		body, _ := io.ReadAll(r.Body)
		if r.Method != http.MethodPatch || r.URL.Path != "/internal/rag/evaluation/issues/"+issueID || !bytes.Contains(body, []byte(`"expected_version":1`)) || !bytes.Contains(body, []byte(`"status":"in_progress"`)) {
			t.Fatalf("quality issue mutation contract lost: %s %s %s", r.Method, r.URL.Path, body)
		}
		return jsonResponse(`{"ok":true,"data":{"issue":{"id":"` + issueID + `","candidate_id":"` + strings.Repeat("b", 64) + `","trace_id":"22222222-2222-4222-8222-222222222222","gate_id":"gate-v1","cohort_id":"cohort-v1","failure_type":"candidate_evidence_missed","owner":"candidate_recall","status":"in_progress","occurrence_count":1,"first_seen_run_id":"11111111-1111-4111-8111-111111111111","last_seen_run_id":"11111111-1111-4111-8111-111111111111","verified_run_id":null,"resolution_note":"","version":2,"created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:01:00Z"}}}`), nil
	})})
	recorder := httptest.NewRecorder()
	NewRagHandler(client).EvaluationQualityIssue(recorder, httptest.NewRequest(http.MethodPatch, "/api/rag/evaluation/issues/"+issueID, strings.NewReader(`{"expected_version":1,"owner":"candidate_recall","status":"in_progress","resolution_note":""}`)), issueID)
	if recorder.Code != http.StatusOK || !bytes.Contains(recorder.Body.Bytes(), []byte(`"version":2`)) {
		t.Fatalf("unexpected issue response: %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestRagQualityIssueLedgerIsBoundedAndRedacted(t *testing.T) {
	issueID := "44444444-4444-4444-8444-444444444444"
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet || r.URL.Path != "/internal/rag/evaluation/issues" || r.URL.Query().Get("status") != "verified" || r.URL.Query().Get("owner") != "all" || r.URL.Query().Get("failure_type") != "all" || r.URL.Query().Get("limit") != "50" {
			t.Fatalf("quality issue ledger contract lost: %s %s", r.Method, r.URL.String())
		}
		return jsonResponse(`{"ok":true,"data":{"issues":[{"issue":{"id":"` + issueID + `","candidate_id":"` + strings.Repeat("b", 64) + `","trace_id":"22222222-2222-4222-8222-222222222222","gate_id":"gate-v1","cohort_id":"cohort-v1","failure_type":"candidate_evidence_missed","owner":"candidate_recall","status":"verified","occurrence_count":2,"first_seen_run_id":"11111111-1111-4111-8111-111111111111","last_seen_run_id":"11111111-1111-4111-8111-111111111111","verified_run_id":"55555555-5555-4555-8555-555555555555","resolution_note":"fixed","version":3,"created_at":"2026-08-02T00:00:00Z","updated_at":"2026-08-02T00:01:00Z"},"trace_id":"22222222-2222-4222-8222-222222222222","workspace_id":"33333333-3333-4333-8333-333333333333","query_hash":"` + strings.Repeat("a", 64) + `","privacy_status":"approved","label_status":"promoted","review_state":"fixed_regression_sample","first_seen_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","last_seen_revision":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","verified_revision":"cccccccccccccccccccccccccccccccccccccccc"}],"summary":{"total":3,"open":1,"in_progress":0,"resolved":0,"verified":2,"dismissed":0}}}`), nil
	})})
	recorder := httptest.NewRecorder()
	NewRagHandler(client).EvaluationQualityIssues(recorder, httptest.NewRequest(http.MethodGet, "/api/rag/evaluation/issues?status=verified&owner=all&failure_type=all&limit=50", nil))
	if recorder.Code != http.StatusOK || !bytes.Contains(recorder.Body.Bytes(), []byte(`"verified":2`)) || !bytes.Contains(recorder.Body.Bytes(), []byte(`"verified_revision":"cccccccc`)) || bytes.Contains(recorder.Body.Bytes(), []byte("raw_query")) || bytes.Contains(recorder.Body.Bytes(), []byte("raw_chunk")) {
		t.Fatalf("unexpected issue ledger projection: %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestRagHandlerUploadsPdfThroughTypedBoundary(t *testing.T) {
	workspaceID := "11111111-1111-4111-8111-111111111111"
	permissionRequestID := "55555555-5555-4555-8555-555555555555"
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodPost || r.URL.Path != "/internal/rag/documents/upload" {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
			}
			body, _ := io.ReadAll(r.Body)
			if !bytes.Contains(body, []byte(`"workspace_id":"`+workspaceID+`"`)) ||
				!bytes.Contains(body, []byte(`"permission_request_id":"`+permissionRequestID+`"`)) ||
				!bytes.Contains(body, []byte(`"filename":"paper.pdf"`)) ||
				!bytes.Contains(body, []byte(`JVBERi0xLjc`)) {
				t.Fatalf("upload contract lost: %s", body)
			}
			return jsonResponse(`{"ok":true,"data":{"artifact_id":"22222222-2222-4222-8222-222222222222","document_id":"33333333-3333-4333-8333-333333333333","job_id":"44444444-4444-4444-8444-444444444444","status":"queued","uploaded":true,"created":true}}`), nil
		}),
	})
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("workspace_id", workspaceID); err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteField("permission_request_id", permissionRequestID); err != nil {
		t.Fatal(err)
	}
	part, err := writer.CreateFormFile("file", "paper.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := part.Write([]byte("%PDF-1.7\nfixture")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/api/rag/documents", &body)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	recorder := httptest.NewRecorder()
	NewRagHandler(client).Documents(recorder, request)
	if recorder.Code != http.StatusOK || !bytes.Contains(recorder.Body.Bytes(), []byte(`"status":"queued"`)) {
		t.Fatalf("unexpected response: %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestRagHandlerForwardsInvalidPdfSoApprovedRunCanTerminalize(t *testing.T) {
	workspaceID := "11111111-1111-4111-8111-111111111111"
	permissionRequestID := "55555555-5555-4555-8555-555555555555"
	requests := 0
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			requests++
			if r.Method != http.MethodPost || r.URL.Path != "/internal/rag/documents/upload" {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
			}
			return jsonResponse(`{"ok":false,"error":{"code":"RAG_UPLOAD_PDF_INVALID","message":"上传内容不是有效 PDF","category":"validation","recoverable":false}}`), nil
		}),
	})
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	if err := writer.WriteField("workspace_id", workspaceID); err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteField("permission_request_id", permissionRequestID); err != nil {
		t.Fatal(err)
	}
	part, err := writer.CreateFormFile("file", "not-a-real.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := part.Write([]byte("not a pdf")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest(http.MethodPost, "/api/rag/documents", &body)
	request.Header.Set("Content-Type", writer.FormDataContentType())
	recorder := httptest.NewRecorder()
	NewRagHandler(client).Documents(recorder, request)

	if requests != 1 || recorder.Code != http.StatusBadRequest ||
		!bytes.Contains(recorder.Body.Bytes(), []byte(`"code":"RAG_UPLOAD_PDF_INVALID"`)) {
		t.Fatalf("unexpected response: requests=%d code=%d body=%s", requests, recorder.Code, recorder.Body.String())
	}
}

func TestRagUploadPermissionIsCreatedAndResolvedBeforeUpload(t *testing.T) {
	workspaceID := "11111111-1111-4111-8111-111111111111"
	requestID := "55555555-5555-4555-8555-555555555555"
	requests := 0
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			requests++
			body, _ := io.ReadAll(r.Body)
			switch requests {
			case 1:
				if r.Method != http.MethodPost || r.URL.Path != "/internal/rag/upload-requests" ||
					!bytes.Contains(body, []byte(`"workspace_id":"`+workspaceID+`"`)) ||
					!bytes.Contains(body, []byte(`"filename":"paper.pdf"`)) ||
					!bytes.Contains(body, []byte(`"size_bytes":8`)) ||
					!bytes.Contains(body, []byte(`"content_sha256":"`+strings.Repeat("a", 64)+`"`)) {
					t.Fatalf("create upload permission contract lost: %s %s", r.URL.Path, body)
				}
			case 2:
				if r.Method != http.MethodPost || r.URL.Path != "/internal/rag/upload-requests/"+requestID+"/resolve" ||
					!bytes.Contains(body, []byte(`"decision":"allow_once"`)) {
					t.Fatalf("resolve upload permission contract lost: %s %s", r.URL.Path, body)
				}
			default:
				t.Fatalf("unexpected request count: %d", requests)
			}
			status := "pending"
			decision := ""
			if requests == 2 {
				status = "approved"
				decision = `,"decision":"allow_once"`
			}
			return jsonResponse(`{"ok":true,"data":{"id":"` + requestID + `","task_id":"22222222-2222-4222-8222-222222222222","run_id":"33333333-3333-4333-8333-333333333333","tool_name":"rag.upload_pdf","action_summary":"upload","risk_level":"L2","scope":{"type":"once"},"arguments_summary":{"filename":"paper.pdf"},"allowed_decisions":["allow_once","deny"],"created_at":"2026-08-05T00:00:00Z","status":"` + status + `"` + decision + `}}`), nil
		}),
	})
	handler := NewRagHandler(client)
	createRecorder := httptest.NewRecorder()
	handler.UploadRequests(createRecorder, httptest.NewRequest(http.MethodPost, "/api/rag/upload-requests", strings.NewReader(`{"workspace_id":"`+workspaceID+`","filename":"paper.pdf","size_bytes":8,"content_sha256":"`+strings.Repeat("a", 64)+`"}`)))
	if createRecorder.Code != http.StatusOK || !bytes.Contains(createRecorder.Body.Bytes(), []byte(`"status":"pending"`)) {
		t.Fatalf("unexpected create response: %d %s", createRecorder.Code, createRecorder.Body.String())
	}
	resolveRecorder := httptest.NewRecorder()
	handler.ResolveUploadRequest(resolveRecorder, httptest.NewRequest(http.MethodPost, "/api/rag/upload-requests/"+requestID+"/resolve", strings.NewReader(`{"decision":"allow_once"}`)), requestID)
	if resolveRecorder.Code != http.StatusOK || !bytes.Contains(resolveRecorder.Body.Bytes(), []byte(`"status":"approved"`)) {
		t.Fatalf("unexpected resolve response: %d %s", resolveRecorder.Code, resolveRecorder.Body.String())
	}
}

func TestRagHandlerRestartsPersistedJobWithoutUpload(t *testing.T) {
	workspaceID := "11111111-1111-4111-8111-111111111111"
	documentID := "22222222-2222-4222-8222-222222222222"
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			if r.Method != http.MethodPost || r.URL.Path != "/internal/rag/documents/"+documentID+"/restart" {
				t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
			}
			body, _ := io.ReadAll(r.Body)
			if !bytes.Contains(body, []byte(`"workspace_id":"`+workspaceID+`"`)) ||
				!bytes.Contains(body, []byte(`"expected_version":2`)) {
				t.Fatalf("restart contract lost: %s", body)
			}
			return jsonResponse(`{"ok":true,"data":{"document_id":"` + documentID + `","job_id":"33333333-3333-4333-8333-333333333333","status":"queued"}}`), nil
		}),
	})
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(
		http.MethodPost,
		"/api/rag/documents/"+documentID+"/restart",
		strings.NewReader(`{"workspace_id":"`+workspaceID+`","expected_version":2}`),
	)
	NewRagHandler(client).Item(recorder, request, documentID, "restart")

	if recorder.Code != http.StatusOK || !bytes.Contains(recorder.Body.Bytes(), []byte(`"status":"queued"`)) {
		t.Fatalf("unexpected response: %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestRagHandlerUpdatesAndCancelsThroughTypedBoundary(t *testing.T) {
	workspaceID := "11111111-1111-4111-8111-111111111111"
	documentID := "22222222-2222-4222-8222-222222222222"
	requests := 0
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			requests++
			body, _ := io.ReadAll(r.Body)
			switch requests {
			case 1:
				if r.Method != http.MethodPatch || r.URL.Path != "/internal/rag/documents/"+documentID ||
					!bytes.Contains(body, []byte(`"expected_version":2`)) ||
					!bytes.Contains(body, []byte(`"enabled":false`)) {
					t.Fatalf("update contract lost: %s %s %s", r.Method, r.URL.Path, body)
				}
				return jsonResponse(`{"ok":true,"data":{"document_id":"` + documentID + `","status":"disabled","version":3}}`), nil
			case 2:
				if r.Method != http.MethodPost || r.URL.Path != "/internal/rag/documents/"+documentID+"/cancel" ||
					!bytes.Contains(body, []byte(`"expected_version":3`)) {
					t.Fatalf("cancel contract lost: %s %s %s", r.Method, r.URL.Path, body)
				}
				return jsonResponse(`{"ok":true,"data":{"document_id":"` + documentID + `","status":"failed","version":4,"job_id":"33333333-3333-4333-8333-333333333333","job_status":"cancelled"}}`), nil
			default:
				t.Fatalf("unexpected request count: %d", requests)
				return nil, nil
			}
		}),
	})

	updateRecorder := httptest.NewRecorder()
	updateRequest := httptest.NewRequest(
		http.MethodPatch,
		"/api/rag/documents/"+documentID,
		strings.NewReader(`{"workspace_id":"`+workspaceID+`","expected_version":2,"enabled":false}`),
	)
	NewRagHandler(client).Item(updateRecorder, updateRequest, documentID, "")
	if updateRecorder.Code != http.StatusOK || !bytes.Contains(updateRecorder.Body.Bytes(), []byte(`"status":"disabled"`)) {
		t.Fatalf("unexpected update response: %d %s", updateRecorder.Code, updateRecorder.Body.String())
	}

	cancelRecorder := httptest.NewRecorder()
	cancelRequest := httptest.NewRequest(
		http.MethodPost,
		"/api/rag/documents/"+documentID+"/cancel",
		strings.NewReader(`{"workspace_id":"`+workspaceID+`","expected_version":3}`),
	)
	NewRagHandler(client).Item(cancelRecorder, cancelRequest, documentID, "cancel")
	if cancelRecorder.Code != http.StatusOK || !bytes.Contains(cancelRecorder.Body.Bytes(), []byte(`"job_status":"cancelled"`)) {
		t.Fatalf("unexpected cancel response: %d %s", cancelRecorder.Code, cancelRecorder.Body.String())
	}
}

func TestRagHandlerUsesL4DeleteRequestBeforePermanentDeletion(t *testing.T) {
	workspaceID := "11111111-1111-4111-8111-111111111111"
	documentID := "22222222-2222-4222-8222-222222222222"
	requestID := "33333333-3333-4333-8333-333333333333"
	requests := 0
	client := controlplane.NewClientWithHTTPClient("http://control-plane", &http.Client{
		Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
			requests++
			body, _ := io.ReadAll(r.Body)
			if requests == 1 {
				if r.URL.Path != "/internal/rag/documents/"+documentID+"/delete-requests" || !bytes.Contains(body, []byte(`"expected_version":4`)) {
					t.Fatalf("delete request contract lost: %s %s", r.URL.Path, body)
				}
				return jsonResponse(`{"ok":true,"data":{"id":"` + requestID + `","task_id":"44444444-4444-4444-8444-444444444444","run_id":"55555555-5555-4555-8555-555555555555","tool_name":"rag.delete_document","action_summary":"永久删除","risk_level":"L4","scope":{"type":"once"},"arguments_summary":{"document_id":"` + documentID + `"},"allowed_decisions":["allow_once","deny"],"created_at":"2026-07-29T00:00:00Z","status":"pending"}}`), nil
			}
			if r.URL.Path != "/internal/rag/delete-requests/"+requestID+"/resolve" || !bytes.Contains(body, []byte(`"decision":"allow_once"`)) {
				t.Fatalf("delete resolution contract lost: %s %s", r.URL.Path, body)
			}
			return jsonResponse(`{"ok":true,"data":{"permission":{"id":"` + requestID + `","task_id":"44444444-4444-4444-8444-444444444444","run_id":"55555555-5555-4555-8555-555555555555","tool_name":"rag.delete_document","action_summary":"永久删除","risk_level":"L4","scope":{"type":"once"},"arguments_summary":{},"allowed_decisions":["allow_once","deny"],"created_at":"2026-07-29T00:00:00Z","status":"consumed","decision":"allow_once"},"document_id":"` + documentID + `","deleted":true,"cleanup_pending_count":0,"source_artifact_retained":true}}`), nil
		}),
	})

	createRecorder := httptest.NewRecorder()
	createRequest := httptest.NewRequest(http.MethodPost, "/api/rag/documents/"+documentID+"/delete-requests", strings.NewReader(`{"workspace_id":"`+workspaceID+`","expected_version":4}`))
	NewRagHandler(client).Item(createRecorder, createRequest, documentID, "delete-requests")
	if createRecorder.Code != http.StatusOK || !bytes.Contains(createRecorder.Body.Bytes(), []byte(`"risk_level":"L4"`)) {
		t.Fatalf("unexpected create response: %d %s", createRecorder.Code, createRecorder.Body.String())
	}

	resolveRecorder := httptest.NewRecorder()
	resolveRequest := httptest.NewRequest(http.MethodPost, "/api/rag/delete-requests/"+requestID+"/resolve", strings.NewReader(`{"decision":"allow_once"}`))
	NewRagHandler(client).ResolveDelete(resolveRecorder, resolveRequest, requestID)
	if resolveRecorder.Code != http.StatusOK || !bytes.Contains(resolveRecorder.Body.Bytes(), []byte(`"source_artifact_retained":true`)) {
		t.Fatalf("unexpected resolve response: %d %s", resolveRecorder.Code, resolveRecorder.Body.String())
	}
}
