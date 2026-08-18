package controlplane

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	observability "github.com/jarvis-assistant/gateway/internal/observability"
	"net/http"
	"net/url"
)

type RagIngestionJobDTO struct {
	ID                   string            `json:"id"`
	Status               string            `json:"status"`
	Attempts             int               `json:"attempts"`
	MaxAttempts          int               `json:"max_attempts"`
	EmbeddingAttempts    int               `json:"embedding_attempts"`
	EmbeddingMaxAttempts int               `json:"embedding_max_attempts"`
	Progress             RagJobProgressDTO `json:"progress"`
	ErrorCode            string            `json:"error_code,omitempty"`
	NextRetryAt          *string           `json:"next_retry_at,omitempty"`
	StartedAt            *string           `json:"started_at,omitempty"`
	CompletedAt          *string           `json:"completed_at,omitempty"`
	FailedAt             *string           `json:"failed_at,omitempty"`
	CreatedAt            string            `json:"created_at"`
	UpdatedAt            string            `json:"updated_at"`
}
type RagJobProgressDTO struct {
	ActiveExecutor       *string        `json:"active_executor"`
	PageCount            int            `json:"page_count"`
	NativeExtractionDone bool           `json:"native_extraction_done"`
	VisualPagesTotal     int            `json:"visual_pages_total"`
	VisualPagesCompleted int            `json:"visual_pages_completed"`
	VisualRouteCounts    map[string]int `json:"visual_route_counts"`
	ChunksTotal          int            `json:"chunks_total"`
	EmbeddingTotal       int            `json:"embedding_total"`
	EmbeddingCompleted   int            `json:"embedding_completed"`
}
type RagDocumentDTO struct {
	ID                     string              `json:"id"`
	WorkspaceID            string              `json:"workspace_id"`
	SourceArtifactID       string              `json:"source_artifact_id"`
	Title                  string              `json:"title"`
	MimeType               string              `json:"mime_type"`
	Status                 string              `json:"status"`
	IngestionPolicyVersion string              `json:"ingestion_policy_version"`
	ParserVersion          string              `json:"parser_version"`
	ChunkerVersion         string              `json:"chunker_version"`
	EmbeddingProvider      string              `json:"embedding_provider"`
	EmbeddingModel         string              `json:"embedding_model"`
	EmbeddingDimensions    *int                `json:"embedding_dimensions,omitempty"`
	ChunkCount             int                 `json:"chunk_count"`
	IndexedAt              *string             `json:"indexed_at,omitempty"`
	Version                int                 `json:"version"`
	CreatedAt              string              `json:"created_at"`
	UpdatedAt              string              `json:"updated_at"`
	LatestJob              *RagIngestionJobDTO `json:"latest_job,omitempty"`
	IndexState             string              `json:"index_state"`
	IndexStaleReasons      []string            `json:"index_stale_reasons"`
	IndexTarget            map[string]any      `json:"index_target"`
}
type ListRagDocumentsResponse struct {
	Documents []RagDocumentDTO `json:"documents"`
}
type UploadRagDocumentRequest struct {
	WorkspaceID         string `json:"workspace_id"`
	PermissionRequestID string `json:"permission_request_id"`
	Filename            string `json:"filename"`
	ContentBase64       string `json:"content_base64"`
}
type CreateRagUploadRequest struct {
	WorkspaceID   string `json:"workspace_id"`
	Filename      string `json:"filename"`
	SizeBytes     int64  `json:"size_bytes"`
	ContentSHA256 string `json:"content_sha256"`
}
type ResolveRagUploadRequest struct {
	Decision string `json:"decision"`
	Note     string `json:"note,omitempty"`
}
type UploadRagDocumentResponse struct {
	ArtifactID string `json:"artifact_id"`
	DocumentID string `json:"document_id"`
	JobID      string `json:"job_id"`
	Status     string `json:"status"`
	Uploaded   bool   `json:"uploaded"`
	Created    bool   `json:"created"`
}
type RestartRagDocumentRequest struct {
	WorkspaceID     string `json:"workspace_id"`
	ExpectedVersion int    `json:"expected_version"`
}

type UpdateRagDocumentRequest struct {
	WorkspaceID     string `json:"workspace_id"`
	ExpectedVersion int    `json:"expected_version"`
	Enabled         bool   `json:"enabled"`
}
type UpdateRagDocumentResponse struct {
	DocumentID string `json:"document_id"`
	Status     string `json:"status"`
	Version    int    `json:"version"`
}
type CancelRagDocumentRequest struct {
	WorkspaceID     string `json:"workspace_id"`
	ExpectedVersion int    `json:"expected_version"`
}
type CancelRagDocumentResponse struct {
	DocumentID string `json:"document_id"`
	Status     string `json:"status"`
	Version    int    `json:"version"`
	JobID      string `json:"job_id"`
	JobStatus  string `json:"job_status"`
}
type RestartRagDocumentResponse struct {
	DocumentID string `json:"document_id"`
	JobID      string `json:"job_id"`
	Status     string `json:"status"`
}
type CreateRagDeleteRequest struct {
	WorkspaceID     string `json:"workspace_id"`
	ExpectedVersion int    `json:"expected_version"`
}
type ResolveRagDeleteRequest struct {
	Decision string `json:"decision"`
	Note     string `json:"note,omitempty"`
}
type RagDeleteResolutionResponse struct {
	Permission             PermissionRequestDTO `json:"permission"`
	DocumentID             string               `json:"document_id"`
	Deleted                bool                 `json:"deleted"`
	CleanupPendingCount    int                  `json:"cleanup_pending_count"`
	SourceArtifactRetained bool                 `json:"source_artifact_retained"`
}

type RagFeedbackDTO struct {
	ID               string            `json:"id"`
	TraceID          string            `json:"trace_id"`
	WorkspaceID      string            `json:"workspace_id"`
	TaskID           string            `json:"task_id"`
	RunID            string            `json:"run_id"`
	MessageID        string            `json:"message_id"`
	Kind             string            `json:"kind"`
	CitationChunkID  string            `json:"citation_chunk_id,omitempty"`
	Status           string            `json:"status"`
	FailureCategory  string            `json:"failure_category,omitempty"`
	QueryHash        string            `json:"query_hash,omitempty"`
	PipelineVersions map[string]string `json:"pipeline_versions,omitempty"`
	ResultCount      int               `json:"result_count,omitempty"`
	ContextTruncated bool              `json:"context_truncated,omitempty"`
	CreatedAt        string            `json:"created_at"`
	UpdatedAt        string            `json:"updated_at"`
}
type RagFeedbackResponse struct {
	Feedback RagFeedbackDTO `json:"feedback"`
}
type ListRagFeedbackResponse struct {
	Feedback []RagFeedbackDTO `json:"feedback"`
}
type RagFeedbackEvidenceDTO struct {
	ChunkID       string   `json:"chunk_id"`
	DocumentID    string   `json:"document_id"`
	ContentHash   string   `json:"content_hash"`
	CandidateRank *int     `json:"candidate_rank"`
	RerankedRank  *int     `json:"reranked_rank"`
	InContext     bool     `json:"in_context"`
	Sources       []string `json:"sources"`
	Snippet       *string  `json:"snippet"`
}
type RagFeedbackLabelDTO struct {
	ID                   string   `json:"id"`
	Source               string   `json:"source"`
	Status               string   `json:"status"`
	PositiveChunkIDs     []string `json:"positive_chunk_ids"`
	HardNegativeChunkIDs []string `json:"hard_negative_chunk_ids"`
}
type RagFeedbackDetailResponse struct {
	Feedback         RagFeedbackDTO           `json:"feedback"`
	QueryHash        string                   `json:"query_hash"`
	Query            *string                  `json:"query"`
	PrivacyStatus    string                   `json:"privacy_status"`
	PipelineVersions map[string]string        `json:"pipeline_versions"`
	ResultCount      int                      `json:"result_count"`
	ContextTruncated bool                     `json:"context_truncated"`
	Evidence         []RagFeedbackEvidenceDTO `json:"evidence"`
	Label            *RagFeedbackLabelDTO     `json:"label"`
}
type TriageRagFeedbackResponse struct {
	Feedback    RagFeedbackDTO `json:"feedback"`
	LabelStatus string         `json:"label_status,omitempty"`
}
type RagEvaluationTraceDTO struct {
	TraceID           string            `json:"trace_id"`
	WorkspaceID       string            `json:"workspace_id"`
	TaskID            string            `json:"task_id"`
	RunID             string            `json:"run_id"`
	QueryHash         string            `json:"query_hash"`
	PrivacyStatus     string            `json:"privacy_status"`
	LabelStatus       string            `json:"label_status,omitempty"`
	LabelSource       string            `json:"label_source,omitempty"`
	CandidateCount    int               `json:"candidate_count"`
	RerankedCount     int               `json:"reranked_count"`
	ContextChunkCount int               `json:"context_chunk_count"`
	ContextTruncated  bool              `json:"context_truncated"`
	PipelineVersions  map[string]string `json:"pipeline_versions"`
	CreatedAt         string            `json:"created_at"`
}
type ListRagEvaluationTracesResponse struct {
	Traces []RagEvaluationTraceDTO `json:"traces"`
}
type RagQualityGateRunDTO struct {
	ID          string             `json:"id"`
	GateID      string             `json:"gate_id"`
	CohortID    string             `json:"cohort_id"`
	BaselineID  string             `json:"baseline_id"`
	Revision    string             `json:"revision"`
	Status      string             `json:"status"`
	SampleCount int                `json:"sample_count"`
	Metrics     map[string]float64 `json:"metrics"`
	Checks      []map[string]any   `json:"checks"`
	GeneratedAt string             `json:"generated_at"`
}
type RagQualityMetricTrendDTO struct {
	MetricID  string  `json:"metric_id"`
	Current   float64 `json:"current"`
	Previous  float64 `json:"previous"`
	Delta     float64 `json:"delta"`
	Direction string  `json:"direction"`
}
type RagQualityAlertDTO struct {
	Code      string   `json:"code"`
	Severity  string   `json:"severity"`
	SubjectID string   `json:"subject_id"`
	Current   *float64 `json:"current"`
	Previous  *float64 `json:"previous"`
	Delta     *float64 `json:"delta"`
}
type RagQualityFailureClusterDTO struct {
	FailureType     string   `json:"failure_type"`
	Priority        string   `json:"priority"`
	LatestRate      float64  `json:"latest_rate"`
	LatestCount     int      `json:"latest_count"`
	PreviousRate    *float64 `json:"previous_rate"`
	RateDelta       *float64 `json:"rate_delta"`
	OccurrenceCount int      `json:"occurrence_count"`
	Threshold       float64  `json:"threshold"`
	CheckPassed     bool     `json:"check_passed"`
}
type RagQualityGateInsightsDTO struct {
	ComparisonState        string                        `json:"comparison_state"`
	CompatibleHistoryCount int                           `json:"compatible_history_count"`
	PreviousRunID          *string                       `json:"previous_run_id"`
	MetricTrends           []RagQualityMetricTrendDTO    `json:"metric_trends"`
	Alerts                 []RagQualityAlertDTO          `json:"alerts"`
	FailureClusters        []RagQualityFailureClusterDTO `json:"failure_clusters"`
}
type ListRagQualityGateRunsResponse struct {
	Runs     []RagQualityGateRunDTO    `json:"runs"`
	Insights RagQualityGateInsightsDTO `json:"insights"`
}
type RagQualityFailureTargetDTO struct {
	CandidateID    string              `json:"candidate_id"`
	TraceID        string              `json:"trace_id"`
	WorkspaceID    string              `json:"workspace_id"`
	QueryHash      string              `json:"query_hash"`
	FailureType    string              `json:"failure_type"`
	SuspectedStage string              `json:"suspected_stage"`
	Severity       string              `json:"severity"`
	MetricIDs      []string            `json:"metric_ids"`
	PrivacyStatus  string              `json:"privacy_status"`
	LabelStatus    *string             `json:"label_status"`
	LabelSource    *string             `json:"label_source"`
	ReviewState    string              `json:"review_state"`
	Issue          *RagQualityIssueDTO `json:"issue"`
}
type ListRagQualityFailureTargetsResponse struct {
	Targets []RagQualityFailureTargetDTO `json:"targets"`
}
type RagQualityIssueDTO struct {
	ID              string  `json:"id"`
	CandidateID     string  `json:"candidate_id"`
	TraceID         string  `json:"trace_id"`
	GateID          string  `json:"gate_id"`
	CohortID        string  `json:"cohort_id"`
	FailureType     string  `json:"failure_type"`
	Owner           string  `json:"owner"`
	Status          string  `json:"status"`
	OccurrenceCount int     `json:"occurrence_count"`
	FirstSeenRunID  string  `json:"first_seen_run_id"`
	LastSeenRunID   string  `json:"last_seen_run_id"`
	VerifiedRunID   *string `json:"verified_run_id"`
	ResolutionNote  string  `json:"resolution_note"`
	Version         int     `json:"version"`
	CreatedAt       string  `json:"created_at"`
	UpdatedAt       string  `json:"updated_at"`
}
type UpdateRagQualityIssueResponse struct {
	Issue RagQualityIssueDTO `json:"issue"`
}
type RagQualityIssueLedgerItemDTO struct {
	Issue             RagQualityIssueDTO `json:"issue"`
	TraceID           string             `json:"trace_id"`
	WorkspaceID       string             `json:"workspace_id"`
	QueryHash         string             `json:"query_hash"`
	PrivacyStatus     string             `json:"privacy_status"`
	LabelStatus       *string            `json:"label_status"`
	ReviewState       string             `json:"review_state"`
	FirstSeenRevision string             `json:"first_seen_revision"`
	LastSeenRevision  string             `json:"last_seen_revision"`
	VerifiedRevision  *string            `json:"verified_revision"`
}
type RagQualityIssueSummaryDTO struct {
	Total      int `json:"total"`
	Open       int `json:"open"`
	InProgress int `json:"in_progress"`
	Resolved   int `json:"resolved"`
	Verified   int `json:"verified"`
	Dismissed  int `json:"dismissed"`
}
type ListRagQualityIssuesResponse struct {
	Issues  []RagQualityIssueLedgerItemDTO `json:"issues"`
	Summary RagQualityIssueSummaryDTO      `json:"summary"`
}
type RagEvaluationLabelDTO struct {
	ID                   string   `json:"id"`
	Source               string   `json:"source"`
	Status               string   `json:"status"`
	PositiveChunkIDs     []string `json:"positive_chunk_ids"`
	HardNegativeChunkIDs []string `json:"hard_negative_chunk_ids"`
	Notes                string   `json:"notes"`
}
type RagPromotionCandidateDTO struct {
	SchemaVersion           int    `json:"schema_version"`
	TraceID                 string `json:"trace_id"`
	QueryHash               string `json:"query_hash"`
	RawQueryIncluded        bool   `json:"raw_query_included"`
	RawChunkContentIncluded bool   `json:"raw_chunk_content_included"`
}
type RagEvaluationTraceDetailResponse struct {
	Trace              RagEvaluationTraceDTO     `json:"trace"`
	Query              *string                   `json:"query"`
	Request            map[string]any            `json:"request"`
	Evidence           []RagFeedbackEvidenceDTO  `json:"evidence"`
	Label              *RagEvaluationLabelDTO    `json:"label"`
	PromotionCandidate *RagPromotionCandidateDTO `json:"promotion_candidate"`
}

func (c *Client) ListRagDocuments(ctx context.Context, workspaceID string, includeDisabled bool) (*ListRagDocumentsResponse, error) {
	query := url.Values{"workspace_id": []string{workspaceID}}
	if includeDisabled {
		query.Set("include_disabled", "true")
	}
	var resp apiResponse
	if err := c.get(ctx, "/internal/rag/documents?"+query.Encode(), &resp); err != nil {
		return nil, err
	}
	var data ListRagDocumentsResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析 RAG 文档列表响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) CreateRagUploadRequest(ctx context.Context, workspaceID, filename string, sizeBytes int64, contentSHA256 string) (*PermissionRequestDTO, error) {
	input := CreateRagUploadRequest{WorkspaceID: workspaceID, Filename: filename, SizeBytes: sizeBytes, ContentSHA256: contentSHA256}
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPost, "/internal/rag/upload-requests", input, &resp); err != nil {
		return nil, err
	}
	var data PermissionRequestDTO
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析 RAG 上传权限响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ResolveRagUploadRequest(ctx context.Context, requestID, decision, note string) (*PermissionRequestDTO, error) {
	input := ResolveRagUploadRequest{Decision: decision, Note: note}
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/upload-requests/%s/resolve", url.PathEscape(requestID))
	if err := c.sendJSON(ctx, http.MethodPost, path, input, &resp); err != nil {
		return nil, err
	}
	var data PermissionRequestDTO
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析 RAG 上传权限决定响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) UploadRagDocument(ctx context.Context, workspaceID, permissionRequestID, filename string, content []byte) (*UploadRagDocumentResponse, error) {
	input := UploadRagDocumentRequest{
		WorkspaceID:         workspaceID,
		PermissionRequestID: permissionRequestID,
		Filename:            filename,
		ContentBase64:       base64.StdEncoding.EncodeToString(content),
	}
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPost, "/internal/rag/documents/upload", input, &resp); err != nil {
		return nil, err
	}
	var data UploadRagDocumentResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析 RAG 上传响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) RestartRagDocument(ctx context.Context, workspaceID, documentID string, expectedVersion int) (*RestartRagDocumentResponse, error) {
	input := RestartRagDocumentRequest{WorkspaceID: workspaceID, ExpectedVersion: expectedVersion}
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/documents/%s/restart", url.PathEscape(documentID))
	if err := c.sendJSON(ctx, http.MethodPost, path, input, &resp); err != nil {
		return nil, err
	}
	var data RestartRagDocumentResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析 RAG 重新执行响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) UpdateRagDocument(ctx context.Context, workspaceID, documentID string, expectedVersion int, enabled bool) (*UpdateRagDocumentResponse, error) {
	input := UpdateRagDocumentRequest{WorkspaceID: workspaceID, ExpectedVersion: expectedVersion, Enabled: enabled}
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/documents/%s", url.PathEscape(documentID))
	if err := c.sendJSON(ctx, http.MethodPatch, path, input, &resp); err != nil {
		return nil, err
	}
	var data UpdateRagDocumentResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) CancelRagDocument(ctx context.Context, workspaceID, documentID string, expectedVersion int) (*CancelRagDocumentResponse, error) {
	input := CancelRagDocumentRequest{WorkspaceID: workspaceID, ExpectedVersion: expectedVersion}
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/documents/%s/cancel", url.PathEscape(documentID))
	if err := c.sendJSON(ctx, http.MethodPost, path, input, &resp); err != nil {
		return nil, err
	}
	var data CancelRagDocumentResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) CreateRagDeleteRequest(ctx context.Context, workspaceID, documentID string, expectedVersion int) (*PermissionRequestDTO, error) {
	input := CreateRagDeleteRequest{WorkspaceID: workspaceID, ExpectedVersion: expectedVersion}
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/documents/%s/delete-requests", url.PathEscape(documentID))
	if err := c.sendJSON(ctx, http.MethodPost, path, input, &resp); err != nil {
		return nil, err
	}
	var data PermissionRequestDTO
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) ResolveRagDeleteRequest(ctx context.Context, requestID, decision, note string) (*RagDeleteResolutionResponse, error) {
	input := ResolveRagDeleteRequest{Decision: decision, Note: note}
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/delete-requests/%s/resolve", url.PathEscape(requestID))
	if err := c.sendJSON(ctx, http.MethodPost, path, input, &resp); err != nil {
		return nil, err
	}
	var data RagDeleteResolutionResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) SubmitRagFeedback(ctx context.Context, messageID, kind, citationChunkID string) (*RagFeedbackResponse, error) {
	input := map[string]string{"message_id": messageID, "kind": kind}
	if citationChunkID != "" {
		input["citation_chunk_id"] = citationChunkID
	}
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPost, "/internal/rag/feedback", input, &resp); err != nil {
		return nil, err
	}
	var data RagFeedbackResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) ListRagFeedback(ctx context.Context, workspaceID, status string, limit int) (*ListRagFeedbackResponse, error) {
	query := url.Values{"workspace_id": []string{workspaceID}, "status": []string{status}, "limit": []string{fmt.Sprint(limit)}}
	var resp apiResponse
	if err := c.get(ctx, "/internal/rag/feedback?"+query.Encode(), &resp); err != nil {
		return nil, err
	}
	var data ListRagFeedbackResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) ResolveRagFeedback(ctx context.Context, feedbackID, status string) (*RagFeedbackResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/feedback/%s", url.PathEscape(feedbackID))
	if err := c.sendJSON(ctx, http.MethodPatch, path, map[string]string{"status": status}, &resp); err != nil {
		return nil, err
	}
	var data RagFeedbackResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) InspectRagFeedback(ctx context.Context, feedbackID string) (*RagFeedbackDetailResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/feedback/%s", url.PathEscape(feedbackID))
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data RagFeedbackDetailResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) TriageRagFeedback(ctx context.Context, feedbackID, failureCategory string, positives, negatives []string) (*TriageRagFeedbackResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/feedback/%s/triage", url.PathEscape(feedbackID))
	input := map[string]any{"failure_category": failureCategory, "positive_chunk_ids": positives, "hard_negative_chunk_ids": negatives}
	if err := c.sendJSON(ctx, http.MethodPost, path, input, &resp); err != nil {
		return nil, err
	}
	var data TriageRagFeedbackResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) ListRagEvaluationTraces(ctx context.Context, workspaceID, privacyStatus string, limit int) (*ListRagEvaluationTracesResponse, error) {
	query := url.Values{"workspace_id": []string{workspaceID}, "privacy_status": []string{privacyStatus}, "limit": []string{fmt.Sprint(limit)}}
	var resp apiResponse
	if err := c.get(ctx, "/internal/rag/evaluation/traces?"+query.Encode(), &resp); err != nil {
		return nil, err
	}
	var data ListRagEvaluationTracesResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) ListRagQualityGateRuns(ctx context.Context, limit int) (*ListRagQualityGateRunsResponse, error) {
	query := url.Values{"limit": []string{fmt.Sprint(limit)}}
	var resp apiResponse
	if err := c.get(ctx, "/internal/rag/evaluation/gates?"+query.Encode(), &resp); err != nil {
		return nil, err
	}
	var data ListRagQualityGateRunsResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) ListRagQualityFailureTargets(ctx context.Context, runID, failureType string, limit int) (*ListRagQualityFailureTargetsResponse, error) {
	query := url.Values{"failure_type": []string{failureType}, "limit": []string{fmt.Sprint(limit)}}
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/evaluation/gates/%s/failure-targets?%s", url.PathEscape(runID), query.Encode())
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data ListRagQualityFailureTargetsResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) ListRagQualityIssues(ctx context.Context, status, owner, failureType string, limit int) (*ListRagQualityIssuesResponse, error) {
	query := url.Values{"status": []string{status}, "owner": []string{owner}, "failure_type": []string{failureType}, "limit": []string{fmt.Sprint(limit)}}
	var resp apiResponse
	if err := c.get(ctx, "/internal/rag/evaluation/issues?"+query.Encode(), &resp); err != nil {
		return nil, err
	}
	var data ListRagQualityIssuesResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) UpdateRagQualityIssue(ctx context.Context, issueID string, input map[string]any) (*UpdateRagQualityIssueResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/evaluation/issues/%s", url.PathEscape(issueID))
	if err := c.sendJSON(ctx, http.MethodPatch, path, input, &resp); err != nil {
		return nil, err
	}
	var data UpdateRagQualityIssueResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) InspectRagEvaluationTrace(ctx context.Context, traceID, workspaceID string) (*RagEvaluationTraceDetailResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/evaluation/traces/%s?workspace_id=%s", url.PathEscape(traceID), url.QueryEscape(workspaceID))
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data RagEvaluationTraceDetailResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) ReviewRagEvaluationPrivacy(ctx context.Context, traceID, workspaceID, decision string) (*RagEvaluationTraceDetailResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/evaluation/traces/%s/privacy", url.PathEscape(traceID))
	if err := c.sendJSON(ctx, http.MethodPost, path, map[string]string{"workspace_id": workspaceID, "decision": decision}, &resp); err != nil {
		return nil, err
	}
	var data RagEvaluationTraceDetailResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) ReviewRagEvaluationLabel(ctx context.Context, traceID, workspaceID, status string, positives, negatives []string, notes string) (*RagEvaluationTraceDetailResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/evaluation/traces/%s/label", url.PathEscape(traceID))
	input := map[string]any{"workspace_id": workspaceID, "status": status, "positive_chunk_ids": positives, "hard_negative_chunk_ids": negatives, "notes": notes}
	if err := c.sendJSON(ctx, http.MethodPost, path, input, &resp); err != nil {
		return nil, err
	}
	var data RagEvaluationTraceDetailResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) PromoteRagEvaluationTrace(ctx context.Context, traceID, workspaceID string) (*RagEvaluationTraceDetailResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf("/internal/rag/evaluation/traces/%s/promote", url.PathEscape(traceID))
	if err := c.sendJSON(ctx, http.MethodPost, path, map[string]string{"workspace_id": workspaceID}, &resp); err != nil {
		return nil, err
	}
	var data RagEvaluationTraceDetailResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

type McpToolDTO struct {
	ID           string         `json:"id"`
	OriginalName string         `json:"original_name"`
	InternalName string         `json:"internal_name"`
	Description  string         `json:"description"`
	InputSchema  map[string]any `json:"input_schema"`
	RiskLevel    string         `json:"risk_level"`
	Enabled      bool           `json:"enabled"`
}
type McpServerDTO struct {
	ID              string       `json:"id"`
	Slug            string       `json:"slug"`
	Name            string       `json:"name"`
	Transport       string       `json:"transport"`
	Command         string       `json:"command"`
	Args            []string     `json:"args"`
	EnvKeys         []string     `json:"env_keys"`
	Enabled         bool         `json:"enabled"`
	Status          string       `json:"status"`
	LastErrorCode   string       `json:"last_error_code,omitempty"`
	LastConnectedAt string       `json:"last_connected_at,omitempty"`
	Version         int          `json:"version"`
	CreatedAt       string       `json:"created_at"`
	UpdatedAt       string       `json:"updated_at"`
	Tools           []McpToolDTO `json:"tools"`
}
type ListMcpServersResponse struct {
	Servers               []McpServerDTO `json:"servers"`
	WorkerRestartRequired bool           `json:"worker_restart_required,omitempty"`
}
type CreateMcpServerRequest struct {
	Slug    string   `json:"slug"`
	Name    string   `json:"name"`
	Command string   `json:"command"`
	Args    []string `json:"args"`
	EnvKeys []string `json:"env_keys"`
}
type UpdateMcpServerRequest struct {
	Enabled         bool `json:"enabled"`
	ExpectedVersion int  `json:"expected_version"`
}
type McpServerResponse struct {
	Server                McpServerDTO `json:"server"`
	WorkerRestartRequired bool         `json:"worker_restart_required"`
}

func (c *Client) ListMcpServers(ctx context.Context) (*ListMcpServersResponse, error) {
	var resp apiResponse
	if err := c.get(ctx, "/internal/mcp-servers", &resp); err != nil {
		return nil, err
	}
	var data ListMcpServersResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) CreateMcpServer(ctx context.Context, input CreateMcpServerRequest) (*McpServerResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPost, "/internal/mcp-servers", input, &resp); err != nil {
		return nil, err
	}
	var data McpServerResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

func (c *Client) ConnectBuiltinLiteratureServer(ctx context.Context) (*McpServerResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPost, "/internal/mcp-servers/builtin/literature", struct{}{}, &resp); err != nil {
		return nil, err
	}
	var data McpServerResponse
	return &data, json.Unmarshal(resp.Data, &data)
}
func (c *Client) UpdateMcpServer(ctx context.Context, id string, input UpdateMcpServerRequest) (*McpServerResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPatch, "/internal/mcp-servers/"+id, input, &resp); err != nil {
		return nil, err
	}
	var data McpServerResponse
	return &data, json.Unmarshal(resp.Data, &data)
}

// ── Personal Knowledge Base ──

type KnowledgeVaultDTO struct {
	ID            string `json:"id"`
	Name          string `json:"name"`
	RootPath      string `json:"root_path"`
	CanonicalPath string `json:"canonical_path"`
	Status        string `json:"status"`
	Source        string `json:"source"`
	CreatedAt     string `json:"created_at"`
	UpdatedAt     string `json:"updated_at"`
}
type KnowledgeDocumentDTO struct {
	ID           string   `json:"id"`
	VaultID      string   `json:"vault_id"`
	Title        string   `json:"title"`
	Kind         string   `json:"kind"`
	RelativePath string   `json:"relative_path"`
	ContentHash  string   `json:"content_hash"`
	SizeBytes    int64    `json:"size_bytes"`
	Tags         []string `json:"tags"`
	SourceURLs   []string `json:"source_urls"`
	SourceTaskID string   `json:"source_task_id,omitempty"`
	SourceRunID  string   `json:"source_run_id,omitempty"`
	CreatedAt    string   `json:"created_at"`
	UpdatedAt    string   `json:"updated_at"`
}
type ListKnowledgeVaultsResponse struct {
	Vaults        []KnowledgeVaultDTO `json:"vaults"`
	SuggestedPath string              `json:"suggested_path"`
}
type ConnectKnowledgeVaultRequest struct {
	Path string `json:"path"`
}
type KnowledgeVaultResponse struct {
	Vault KnowledgeVaultDTO `json:"vault"`
}
type ListKnowledgeDocumentsResponse struct {
	Documents []KnowledgeDocumentDTO `json:"documents"`
}
type CreateKnowledgeDocumentRequest struct {
	Title      string   `json:"title"`
	Kind       string   `json:"kind"`
	Content    string   `json:"content"`
	Tags       []string `json:"tags"`
	SourceURLs []string `json:"source_urls"`
}

type ScheduledTaskDTO struct {
	ID              string         `json:"id"`
	Name            string         `json:"name"`
	UserGoal        string         `json:"user_goal"`
	Recurrence      string         `json:"recurrence"`
	Timezone        string         `json:"timezone"`
	Hour            int            `json:"hour"`
	Minute          int            `json:"minute"`
	Weekday         *int           `json:"weekday,omitempty"`
	WorkspaceID     string         `json:"workspace_id,omitempty"`
	Status          string         `json:"status"`
	AuthorizedTools []string       `json:"authorized_tools"`
	TaskKind        string         `json:"task_kind"`
	SourcePolicy    map[string]any `json:"source_policy"`
	NextRunAt       string         `json:"next_run_at"`
	LastRunAt       string         `json:"last_run_at,omitempty"`
	LastTaskID      string         `json:"last_task_id,omitempty"`
	LastRunID       string         `json:"last_run_id,omitempty"`
	Version         int            `json:"version"`
	CreatedAt       string         `json:"created_at"`
	UpdatedAt       string         `json:"updated_at"`
}
type ScheduledExecutionDTO struct {
	ID              string `json:"id"`
	ScheduledTaskID string `json:"scheduled_task_id"`
	ScheduledFor    string `json:"scheduled_for"`
	Status          string `json:"status"`
	TaskID          string `json:"task_id,omitempty"`
	RunID           string `json:"run_id,omitempty"`
	Attempts        int    `json:"attempts"`
	ErrorCode       string `json:"error_code,omitempty"`
	CreatedAt       string `json:"created_at"`
	UpdatedAt       string `json:"updated_at"`
}
type ListScheduledTasksResponse struct {
	ScheduledTasks []ScheduledTaskDTO `json:"scheduled_tasks"`
}
type CreateScheduledTaskRequest struct {
	Name             string `json:"name"`
	UserGoal         string `json:"user_goal"`
	Recurrence       string `json:"recurrence"`
	Timezone         string `json:"timezone"`
	Hour             int    `json:"hour"`
	Minute           int    `json:"minute"`
	Weekday          *int   `json:"weekday,omitempty"`
	WorkspaceID      string `json:"workspace_id,omitempty"`
	TaskKind         string `json:"task_kind"`
	SourceQuery      string `json:"source_query,omitempty"`
	SourceMaxResults int    `json:"source_max_results"`
}
type UpdateScheduledTaskRequest struct {
	ExpectedVersion int    `json:"expected_version"`
	Status          string `json:"status"`
}
type ScheduledTaskResponse struct {
	ScheduledTask ScheduledTaskDTO `json:"scheduled_task"`
}
type ScheduledExecutionResponse struct {
	Execution ScheduledExecutionDTO `json:"execution"`
}

func (c *Client) ListScheduledTasks(ctx context.Context) (*ListScheduledTasksResponse, error) {
	var resp apiResponse
	if err := c.get(ctx, "/internal/scheduled-tasks", &resp); err != nil {
		return nil, err
	}
	var data ListScheduledTasksResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析定期任务失败: %w", err)
	}
	return &data, nil
}
func (c *Client) CreateScheduledTask(ctx context.Context, input CreateScheduledTaskRequest) (*ScheduledTaskResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPost, "/internal/scheduled-tasks", input, &resp); err != nil {
		return nil, err
	}
	var data ScheduledTaskResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, err
	}
	return &data, nil
}
func (c *Client) UpdateScheduledTask(ctx context.Context, id string, input UpdateScheduledTaskRequest) (*ScheduledTaskResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPatch, "/internal/scheduled-tasks/"+id, input, &resp); err != nil {
		return nil, err
	}
	var data ScheduledTaskResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, err
	}
	return &data, nil
}
func (c *Client) TriggerScheduledTask(ctx context.Context, id string) (*ScheduledExecutionResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/scheduled-tasks/"+id+"/trigger", nil, &resp); err != nil {
		return nil, err
	}
	var data ScheduledExecutionResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, err
	}
	return &data, nil
}

type KnowledgeDocumentResponse struct {
	Document KnowledgeDocumentDTO `json:"document"`
}

func (c *Client) ListKnowledgeVaults(ctx context.Context) (*ListKnowledgeVaultsResponse, error) {
	var resp apiResponse
	if err := c.get(ctx, "/internal/knowledge-vaults", &resp); err != nil {
		return nil, err
	}
	var data ListKnowledgeVaultsResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析知识库列表响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ConnectKnowledgeVault(ctx context.Context, input ConnectKnowledgeVaultRequest) (*KnowledgeVaultResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPost, "/internal/knowledge-vaults/connect", input, &resp); err != nil {
		return nil, err
	}
	var data KnowledgeVaultResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析知识库连接响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ListKnowledgeDocuments(ctx context.Context, vaultID string) (*ListKnowledgeDocumentsResponse, error) {
	var resp apiResponse
	if err := c.get(ctx, "/internal/knowledge-vaults/"+vaultID+"/documents", &resp); err != nil {
		return nil, err
	}
	var data ListKnowledgeDocumentsResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析知识文档列表响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) CreateKnowledgeDocument(ctx context.Context, vaultID string, input CreateKnowledgeDocumentRequest) (*KnowledgeDocumentResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPost, "/internal/knowledge-vaults/"+vaultID+"/documents", input, &resp); err != nil {
		return nil, err
	}
	var data KnowledgeDocumentResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析知识文档响应失败: %w", err)
	}
	return &data, nil
}

// ── Workspace ──

type WorkspaceDTO struct {
	ID            string  `json:"id"`
	Name          string  `json:"name"`
	RootPath      string  `json:"root_path"`
	CanonicalPath string  `json:"canonical_path"`
	Status        string  `json:"status"`
	Source        string  `json:"source"`
	CreatedAt     string  `json:"created_at"`
	UpdatedAt     string  `json:"updated_at"`
	RevokedAt     *string `json:"revoked_at,omitempty"`
}

type ListWorkspacesResponse struct {
	Workspaces []WorkspaceDTO `json:"workspaces"`
}

type PickWorkspaceResponse struct {
	Workspace *WorkspaceDTO `json:"workspace"`
	Cancelled bool          `json:"cancelled"`
}

type RevokeWorkspaceResponse struct {
	Workspace WorkspaceDTO `json:"workspace"`
}

// ── Long-term Memory ──

type MemoryDTO struct {
	ID          string `json:"id"`
	ScopeType   string `json:"scope_type"`
	WorkspaceID string `json:"workspace_id,omitempty"`
	Category    string `json:"category"`
	Key         string `json:"key"`
	Content     string `json:"content"`
	Status      string `json:"status"`
	SourceType  string `json:"source_type"`
	Importance  int    `json:"importance"`
	Version     int    `json:"version"`
	CreatedAt   string `json:"created_at"`
	UpdatedAt   string `json:"updated_at"`
}
type ListMemoriesResponse struct {
	Memories []MemoryDTO `json:"memories"`
}
type MemoryResponse struct {
	Memory MemoryDTO `json:"memory"`
}
type CreateMemoryRequest struct {
	ScopeType   string `json:"scope_type"`
	WorkspaceID string `json:"workspace_id,omitempty"`
	Category    string `json:"category"`
	Key         string `json:"key"`
	Content     string `json:"content"`
	Importance  int    `json:"importance"`
}
type UpdateMemoryRequest struct {
	ExpectedVersion int     `json:"expected_version"`
	Content         *string `json:"content,omitempty"`
	Status          *string `json:"status,omitempty"`
	Importance      *int    `json:"importance,omitempty"`
}

type MemoryCandidateDTO struct {
	ID                      string  `json:"id"`
	ScopeType               string  `json:"scope_type"`
	WorkspaceID             string  `json:"workspace_id,omitempty"`
	Category                string  `json:"category"`
	SuggestedKey            string  `json:"suggested_key"`
	Content                 string  `json:"content"`
	Status                  string  `json:"status"`
	SourceTaskID            string  `json:"source_task_id"`
	SourceRunID             string  `json:"source_run_id"`
	Confidence              float64 `json:"confidence"`
	Importance              int     `json:"importance"`
	Sensitivity             string  `json:"sensitivity"`
	ConflictMemoryID        string  `json:"conflict_memory_id,omitempty"`
	ApprovedMemoryID        string  `json:"approved_memory_id,omitempty"`
	ExtractionPolicyVersion string  `json:"extraction_policy_version"`
	ExpiresAt               string  `json:"expires_at,omitempty"`
	ResolvedAt              string  `json:"resolved_at,omitempty"`
	ResolutionNote          string  `json:"resolution_note,omitempty"`
	Version                 int     `json:"version"`
	CreatedAt               string  `json:"created_at"`
	UpdatedAt               string  `json:"updated_at"`
}

type ListMemoryCandidatesResponse struct {
	Candidates []MemoryCandidateDTO `json:"candidates"`
}
type MemoryCandidateResponse struct {
	Candidate MemoryCandidateDTO `json:"candidate"`
}
type ApproveMemoryCandidateResponse struct {
	Candidate MemoryCandidateDTO `json:"candidate"`
	Memory    MemoryDTO          `json:"memory"`
}
type UpdateMemoryCandidateRequest struct {
	ExpectedVersion int     `json:"expected_version"`
	ScopeType       *string `json:"scope_type,omitempty"`
	WorkspaceID     *string `json:"workspace_id,omitempty"`
	Category        *string `json:"category,omitempty"`
	SuggestedKey    *string `json:"suggested_key,omitempty"`
	Content         *string `json:"content,omitempty"`
	Importance      *int    `json:"importance,omitempty"`
}
type ResolveMemoryCandidateRequest struct {
	ExpectedVersion int    `json:"expected_version"`
	Note            string `json:"note,omitempty"`
}

func (c *Client) ListMemoryCandidates(ctx context.Context, rawQuery string) (*ListMemoryCandidatesResponse, error) {
	path := "/internal/memory-candidates"
	if rawQuery != "" {
		path += "?" + rawQuery
	}
	var resp apiResponse
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data ListMemoryCandidatesResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析记忆候选列表失败: %w", err)
	}
	return &data, nil
}

func (c *Client) UpdateMemoryCandidate(ctx context.Context, id string, input UpdateMemoryCandidateRequest) (*MemoryCandidateResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPatch, "/internal/memory-candidates/"+id, input, &resp); err != nil {
		return nil, err
	}
	var data MemoryCandidateResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析记忆候选更新响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ApproveMemoryCandidate(ctx context.Context, id string, input ResolveMemoryCandidateRequest) (*ApproveMemoryCandidateResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/memory-candidates/"+id+"/approve", input, &resp); err != nil {
		return nil, err
	}
	var data ApproveMemoryCandidateResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析记忆候选批准响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) RejectMemoryCandidate(ctx context.Context, id string, input ResolveMemoryCandidateRequest) (*MemoryCandidateResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/memory-candidates/"+id+"/reject", input, &resp); err != nil {
		return nil, err
	}
	var data MemoryCandidateResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析记忆候选拒绝响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ListMemories(ctx context.Context, rawQuery string) (*ListMemoriesResponse, error) {
	path := "/internal/memories"
	if rawQuery != "" {
		path += "?" + rawQuery
	}
	var resp apiResponse
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data ListMemoriesResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析记忆列表失败: %w", err)
	}
	return &data, nil
}

func (c *Client) CreateMemory(ctx context.Context, input CreateMemoryRequest) (*MemoryResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/memories", input, &resp); err != nil {
		return nil, err
	}
	var data MemoryResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析记忆创建响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) UpdateMemory(ctx context.Context, id string, input UpdateMemoryRequest) (*MemoryResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodPatch, "/internal/memories/"+id, input, &resp); err != nil {
		return nil, err
	}
	var data MemoryResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析记忆更新响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) DeleteMemory(ctx context.Context, id string) (*MemoryResponse, error) {
	var resp apiResponse
	if err := c.sendJSON(ctx, http.MethodDelete, "/internal/memories/"+id, nil, &resp); err != nil {
		return nil, err
	}
	var data MemoryResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析记忆删除响应失败: %w", err)
	}
	return &data, nil
}

// ── Audit Log ──

// AuditLogDTO 是 Python Application 返回的安全审计投影，绝不承载原始 details/error。
type AuditLogDTO struct {
	ID                 string                 `json:"id"`
	EventType          string                 `json:"event_type"`
	Actor              string                 `json:"actor"`
	ActionSummary      string                 `json:"action_summary"`
	TaskID             string                 `json:"task_id,omitempty"`
	RunID              string                 `json:"run_id,omitempty"`
	StepID             string                 `json:"step_id,omitempty"`
	ToolCallID         string                 `json:"tool_call_id,omitempty"`
	RiskLevel          string                 `json:"risk_level,omitempty"`
	PermissionDecision string                 `json:"permission_decision,omitempty"`
	ResultSummary      string                 `json:"result_summary,omitempty"`
	ErrorCode          string                 `json:"error_code,omitempty"`
	DetailsSummary     map[string]interface{} `json:"details_summary"`
	CreatedAt          string                 `json:"created_at"`
}

type ListAuditLogsResponse struct {
	AuditLogs  []AuditLogDTO `json:"audit_logs"`
	NextCursor *string       `json:"next_cursor,omitempty"`
}

type ListAuditLogsRequest struct {
	Limit     int
	EventType string
	Actor     string
	TaskID    string
	RunID     string
	Before    string
}

type ExportAuditLogsRequest struct {
	Format    string
	MaxRows   int
	MaxBytes  int
	EventType string
	Actor     string
	TaskID    string
	RunID     string
	Before    string
}

type PreviewAuditRetentionRequest struct {
	StandardDays  int
	ExtendedDays  int
	MaxScan       int
	MaxCandidates int
}

type AuditRetentionPreviewResponse struct {
	DryRun                  bool   `json:"dry_run"`
	StandardDays            int    `json:"standard_days"`
	ExtendedDays            int    `json:"extended_days"`
	StandardBefore          string `json:"standard_before"`
	ExtendedBefore          string `json:"extended_before"`
	MaxScan                 int    `json:"max_scan"`
	MaxCandidates           int    `json:"max_candidates"`
	ScannedRecords          int    `json:"scanned_records"`
	CandidateRecords        int    `json:"candidate_records"`
	ProtectedRecords        int    `json:"protected_records"`
	ExtendedRetainedRecords int    `json:"extended_retained_records"`
	HasMore                 bool   `json:"has_more"`
}

type CreateAuditRetentionRequest struct {
	StandardDays  int `json:"standard_days"`
	ExtendedDays  int `json:"extended_days"`
	MaxScan       int `json:"max_scan"`
	MaxCandidates int `json:"max_candidates"`
}

type CreateAuditRetentionResponse struct {
	Request PermissionRequestDTO `json:"request"`
}

type ResolveAuditRetentionRequest struct {
	Decision string `json:"decision"`
	Note     string `json:"note,omitempty"`
}

type AuditRetentionResolutionResponse struct {
	Permission     PermissionRequestDTO `json:"permission"`
	DeletedRecords int                  `json:"deleted_records"`
	HasMore        bool                 `json:"has_more"`
}

func (c *Client) ListAuditLogs(ctx context.Context, input ListAuditLogsRequest) (*ListAuditLogsResponse, error) {
	u, err := url.Parse(c.baseURL + "/internal/audit-logs")
	if err != nil {
		return nil, fmt.Errorf("构造审计查询 URL 失败: %w", err)
	}
	q := u.Query()
	q.Set("limit", fmt.Sprintf("%d", input.Limit))
	if input.EventType != "" {
		q.Set("event_type", input.EventType)
	}
	if input.Actor != "" {
		q.Set("actor", input.Actor)
	}
	if input.TaskID != "" {
		q.Set("task_id", input.TaskID)
	}
	if input.RunID != "" {
		q.Set("run_id", input.RunID)
	}
	if input.Before != "" {
		q.Set("before", input.Before)
	}
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("创建审计查询请求失败: %w", err)
	}
	if traceID := observability.TraceIDFromContext(ctx); traceID != "" {
		req.Header.Set("X-Trace-ID", traceID)
	}
	if requestID := observability.RequestIDFromContext(ctx); requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	var resp apiResponse
	if err := c.do(req, &resp); err != nil {
		return nil, err
	}
	var data ListAuditLogsResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析审计查询响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ExportAuditLogs(ctx context.Context, input ExportAuditLogsRequest) (*http.Response, error) {
	u, err := url.Parse(c.baseURL + "/internal/audit-logs/export")
	if err != nil {
		return nil, fmt.Errorf("构造审计导出 URL 失败: %w", err)
	}
	q := u.Query()
	q.Set("format", input.Format)
	q.Set("max_rows", fmt.Sprintf("%d", input.MaxRows))
	q.Set("max_bytes", fmt.Sprintf("%d", input.MaxBytes))
	if input.EventType != "" {
		q.Set("event_type", input.EventType)
	}
	if input.Actor != "" {
		q.Set("actor", input.Actor)
	}
	if input.TaskID != "" {
		q.Set("task_id", input.TaskID)
	}
	if input.RunID != "" {
		q.Set("run_id", input.RunID)
	}
	if input.Before != "" {
		q.Set("before", input.Before)
	}
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("创建审计导出请求失败: %w", err)
	}
	if traceID := observability.TraceIDFromContext(ctx); traceID != "" {
		req.Header.Set("X-Trace-ID", traceID)
	}
	if requestID := observability.RequestIDFromContext(ctx); requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	return c.doStream(req)
}

func (c *Client) PreviewAuditRetention(ctx context.Context, input PreviewAuditRetentionRequest) (*AuditRetentionPreviewResponse, error) {
	u, err := url.Parse(c.baseURL + "/internal/audit-logs/retention/preview")
	if err != nil {
		return nil, fmt.Errorf("构造审计保留预演 URL 失败: %w", err)
	}
	q := u.Query()
	q.Set("standard_days", fmt.Sprintf("%d", input.StandardDays))
	q.Set("extended_days", fmt.Sprintf("%d", input.ExtendedDays))
	q.Set("max_scan", fmt.Sprintf("%d", input.MaxScan))
	q.Set("max_candidates", fmt.Sprintf("%d", input.MaxCandidates))
	u.RawQuery = q.Encode()

	var resp apiResponse
	if err := c.get(ctx, u.RequestURI(), &resp); err != nil {
		return nil, err
	}
	var data AuditRetentionPreviewResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析审计保留预演响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) CreateAuditRetentionRequest(
	ctx context.Context,
	input CreateAuditRetentionRequest,
) (*CreateAuditRetentionResponse, error) {
	var resp apiResponse
	if err := c.post(ctx, "/internal/audit-logs/retention/requests", input, &resp); err != nil {
		return nil, err
	}
	var data CreateAuditRetentionResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析审计保留确认响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ResolveAuditRetentionRequest(
	ctx context.Context,
	requestID string,
	input ResolveAuditRetentionRequest,
) (*AuditRetentionResolutionResponse, error) {
	var resp apiResponse
	path := fmt.Sprintf(
		"/internal/audit-logs/retention/requests/%s/resolve",
		url.PathEscape(requestID),
	)
	if err := c.post(ctx, path, input, &resp); err != nil {
		return nil, err
	}
	var data AuditRetentionResolutionResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析审计保留执行响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) ListWorkspaces(ctx context.Context, includeRevoked bool) (*ListWorkspacesResponse, error) {
	path := fmt.Sprintf("/internal/workspaces?include_revoked=%v", includeRevoked)
	var resp apiResponse
	if err := c.get(ctx, path, &resp); err != nil {
		return nil, err
	}
	var data ListWorkspacesResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析工作区列表响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) PickWorkspace(ctx context.Context) (*PickWorkspaceResponse, error) {
	var resp apiResponse
	if err := c.postWithClient(ctx, "/internal/workspaces/pick", nil, &resp, c.pickerHTTPClient); err != nil {
		return nil, err
	}
	var data PickWorkspaceResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析 pick workspace 响应失败: %w", err)
	}
	return &data, nil
}

func (c *Client) RevokeWorkspace(ctx context.Context, workspaceID string) (*RevokeWorkspaceResponse, error) {
	path := fmt.Sprintf("/internal/workspaces/%s", workspaceID)
	// Control Plane DELETE 用 get 不行，需要 delete
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, c.baseURL+path, nil)
	if err != nil {
		return nil, fmt.Errorf("创建 revoke workspace 请求失败: %w", err)
	}
	if traceID := observability.TraceIDFromContext(ctx); traceID != "" {
		req.Header.Set("X-Trace-ID", traceID)
	}
	if requestID := observability.RequestIDFromContext(ctx); requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	var resp apiResponse
	if err := c.do(req, &resp); err != nil {
		return nil, err
	}
	var data RevokeWorkspaceResponse
	if err := json.Unmarshal(resp.Data, &data); err != nil {
		return nil, fmt.Errorf("解析 revoke workspace 响应失败: %w", err)
	}
	return &data, nil
}
