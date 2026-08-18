package handlers

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"path/filepath"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type RagControlPlane interface {
	ListRagDocuments(context.Context, string, bool) (*controlplane.ListRagDocumentsResponse, error)
	CreateRagUploadRequest(context.Context, string, string, int64, string) (*controlplane.PermissionRequestDTO, error)
	ResolveRagUploadRequest(context.Context, string, string, string) (*controlplane.PermissionRequestDTO, error)
	UploadRagDocument(context.Context, string, string, string, []byte) (*controlplane.UploadRagDocumentResponse, error)
	RestartRagDocument(context.Context, string, string, int) (*controlplane.RestartRagDocumentResponse, error)
	UpdateRagDocument(context.Context, string, string, int, bool) (*controlplane.UpdateRagDocumentResponse, error)
	CancelRagDocument(context.Context, string, string, int) (*controlplane.CancelRagDocumentResponse, error)
	CreateRagDeleteRequest(context.Context, string, string, int) (*controlplane.PermissionRequestDTO, error)
	ResolveRagDeleteRequest(context.Context, string, string, string) (*controlplane.RagDeleteResolutionResponse, error)
	SubmitRagFeedback(context.Context, string, string, string) (*controlplane.RagFeedbackResponse, error)
	ListRagFeedback(context.Context, string, string, int) (*controlplane.ListRagFeedbackResponse, error)
	ResolveRagFeedback(context.Context, string, string) (*controlplane.RagFeedbackResponse, error)
	InspectRagFeedback(context.Context, string) (*controlplane.RagFeedbackDetailResponse, error)
	TriageRagFeedback(context.Context, string, string, []string, []string) (*controlplane.TriageRagFeedbackResponse, error)
	ListRagEvaluationTraces(context.Context, string, string, int) (*controlplane.ListRagEvaluationTracesResponse, error)
	ListRagQualityGateRuns(context.Context, int) (*controlplane.ListRagQualityGateRunsResponse, error)
	ListRagQualityFailureTargets(context.Context, string, string, int) (*controlplane.ListRagQualityFailureTargetsResponse, error)
	ListRagQualityIssues(context.Context, string, string, string, int) (*controlplane.ListRagQualityIssuesResponse, error)
	UpdateRagQualityIssue(context.Context, string, map[string]any) (*controlplane.UpdateRagQualityIssueResponse, error)
	InspectRagEvaluationTrace(context.Context, string, string) (*controlplane.RagEvaluationTraceDetailResponse, error)
	ReviewRagEvaluationPrivacy(context.Context, string, string, string) (*controlplane.RagEvaluationTraceDetailResponse, error)
	ReviewRagEvaluationLabel(context.Context, string, string, string, []string, []string, string) (*controlplane.RagEvaluationTraceDetailResponse, error)
	PromoteRagEvaluationTrace(context.Context, string, string) (*controlplane.RagEvaluationTraceDetailResponse, error)
}

var ragFeedbackKinds = map[string]bool{"helpful": true, "unhelpful": true, "citation_incorrect": true, "evidence_insufficient": true}

func (h *RagHandler) Feedback(w http.ResponseWriter, r *http.Request) {
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 反馈服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	if r.Method == http.MethodGet {
		workspaceID := r.URL.Query().Get("workspace_id")
		status := r.URL.Query().Get("status")
		if status == "" {
			status = "pending"
		}
		limit := 50
		if raw := r.URL.Query().Get("limit"); raw != "" {
			if _, err := fmt.Sscan(raw, &limit); err != nil {
				limit = 0
			}
		}
		if _, err := uuid.Parse(workspaceID); err != nil || (status != "pending" && status != "reviewed" && status != "dismissed") || limit < 1 || limit > 100 {
			writeError(w, 400, "VALIDATION_ERROR", "反馈队列参数无效", "validation", false)
			return
		}
		resp, err := h.controlPlane.ListRagFeedback(ctx, workspaceID, status, limit)
		if err != nil {
			h.writeMutationError(w, err, "RAG_FEEDBACK_LIST_FAILED", "RAG 反馈队列读取失败")
			return
		}
		items := make([]contracts.RagFeedbackDTO, len(resp.Feedback))
		for i, item := range resp.Feedback {
			items[i] = ragFeedbackFromCP(item)
		}
		writeOK(w, contracts.ListRagFeedbackOutput{Feedback: items})
		return
	}
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, "GET, POST")
		return
	}
	var input contracts.SubmitRagFeedbackInput
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&input); err != nil || !ragFeedbackKinds[input.Kind] {
		writeError(w, 400, "VALIDATION_ERROR", "反馈请求无效", "validation", false)
		return
	}
	if _, err := uuid.Parse(string(input.MessageID)); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "message_id 格式无效", "validation", false)
		return
	}
	if input.Kind == "citation_incorrect" {
		if _, err := uuid.Parse(string(input.CitationChunkID)); err != nil {
			writeError(w, 400, "VALIDATION_ERROR", "引用问题必须包含有效 chunk", "validation", false)
			return
		}
	} else if input.CitationChunkID != "" {
		writeError(w, 400, "VALIDATION_ERROR", "非引用反馈不得包含 chunk", "validation", false)
		return
	}
	resp, err := h.controlPlane.SubmitRagFeedback(ctx, string(input.MessageID), input.Kind, string(input.CitationChunkID))
	if err != nil {
		h.writeMutationError(w, err, "RAG_FEEDBACK_SUBMIT_FAILED", "RAG 反馈提交失败")
		return
	}
	writeOK(w, contracts.RagFeedbackMutationOutput{Feedback: ragFeedbackFromCP(resp.Feedback)})
}

func (h *RagHandler) FeedbackItem(w http.ResponseWriter, r *http.Request, feedbackID, action string) {
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 反馈服务不可用", "internal", true)
		return
	}
	if _, err := uuid.Parse(feedbackID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "feedback_id 格式无效", "validation", false)
		return
	}
	if r.Method == http.MethodGet && action == "" {
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		resp, err := h.controlPlane.InspectRagFeedback(ctx, feedbackID)
		if err != nil {
			h.writeMutationError(w, err, "RAG_FEEDBACK_INSPECT_FAILED", "RAG 反馈诊断读取失败")
			return
		}
		writeOK(w, ragFeedbackDetailFromCP(resp))
		return
	}
	if r.Method == http.MethodPost && action == "triage" {
		var input contracts.TriageRagFeedbackInput
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16384)).Decode(&input); err != nil || !ragFailureCategories[input.FailureCategory] || !validRagFeedbackChunkIDs(input.PositiveChunkIDs, input.HardNegativeChunkIDs) {
			writeError(w, 400, "VALIDATION_ERROR", "反馈诊断参数无效", "validation", false)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		resp, err := h.controlPlane.TriageRagFeedback(ctx, feedbackID, input.FailureCategory, idsToStrings(input.PositiveChunkIDs), idsToStrings(input.HardNegativeChunkIDs))
		if err != nil {
			h.writeMutationError(w, err, "RAG_FEEDBACK_TRIAGE_FAILED", "RAG 反馈诊断失败")
			return
		}
		writeOK(w, contracts.TriageRagFeedbackOutput{Feedback: ragFeedbackFromCP(resp.Feedback), LabelStatus: resp.LabelStatus})
		return
	}
	if r.Method != http.MethodPatch {
		WriteMethodNotAllowed(w, "GET, PATCH, POST")
		return
	}
	var input contracts.ResolveRagFeedbackInput
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024)).Decode(&input); err != nil || (input.Status != "reviewed" && input.Status != "dismissed") {
		writeError(w, 400, "VALIDATION_ERROR", "反馈审核结果无效", "validation", false)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ResolveRagFeedback(ctx, feedbackID, input.Status)
	if err != nil {
		h.writeMutationError(w, err, "RAG_FEEDBACK_RESOLVE_FAILED", "RAG 反馈处理失败")
		return
	}
	writeOK(w, contracts.RagFeedbackMutationOutput{Feedback: ragFeedbackFromCP(resp.Feedback)})
}

func ragFeedbackFromCP(value controlplane.RagFeedbackDTO) contracts.RagFeedbackDTO {
	return contracts.RagFeedbackDTO{ID: value.ID, TraceID: value.TraceID, WorkspaceID: value.WorkspaceID, TaskID: value.TaskID, RunID: value.RunID, MessageID: value.MessageID, Kind: value.Kind, CitationChunkID: value.CitationChunkID, Status: value.Status, FailureCategory: value.FailureCategory, QueryHash: value.QueryHash, PipelineVersions: value.PipelineVersions, ResultCount: value.ResultCount, ContextTruncated: value.ContextTruncated, CreatedAt: value.CreatedAt, UpdatedAt: value.UpdatedAt}
}

var ragFailureCategories = map[string]bool{"candidate_miss": true, "reranker_miss": true, "context_omission": true, "context_truncated": true, "citation_mismatch": true, "answer_generation": true, "insufficient_evidence": true, "other": true}

func idsToStrings(values []contracts.ID) []string {
	output := make([]string, len(values))
	for i, value := range values {
		output[i] = string(value)
	}
	return output
}
func validRagFeedbackChunkIDs(groups ...[]contracts.ID) bool {
	total := 0
	for _, group := range groups {
		total += len(group)
		for _, value := range group {
			if _, err := uuid.Parse(string(value)); err != nil {
				return false
			}
		}
	}
	return total <= 200
}
func ragFeedbackDetailFromCP(value *controlplane.RagFeedbackDetailResponse) contracts.RagFeedbackDetailOutput {
	evidence := make([]contracts.RagFeedbackEvidenceDTO, len(value.Evidence))
	for i, item := range value.Evidence {
		evidence[i] = contracts.RagFeedbackEvidenceDTO{ChunkID: contracts.ID(item.ChunkID), DocumentID: contracts.ID(item.DocumentID), ContentHash: item.ContentHash, CandidateRank: item.CandidateRank, RerankedRank: item.RerankedRank, InContext: item.InContext, Sources: item.Sources, Snippet: item.Snippet}
	}
	var label *contracts.RagFeedbackLabelDTO
	if value.Label != nil {
		label = &contracts.RagFeedbackLabelDTO{ID: contracts.ID(value.Label.ID), Source: value.Label.Source, Status: value.Label.Status, PositiveChunkIDs: stringsToIDs(value.Label.PositiveChunkIDs), HardNegativeChunkIDs: stringsToIDs(value.Label.HardNegativeChunkIDs)}
	}
	return contracts.RagFeedbackDetailOutput{Feedback: ragFeedbackFromCP(value.Feedback), QueryHash: value.QueryHash, Query: value.Query, PrivacyStatus: value.PrivacyStatus, PipelineVersions: value.PipelineVersions, ResultCount: value.ResultCount, ContextTruncated: value.ContextTruncated, Evidence: evidence, Label: label}
}
func stringsToIDs(values []string) []contracts.ID {
	output := make([]contracts.ID, len(values))
	for i, value := range values {
		output[i] = contracts.ID(value)
	}
	return output
}

var ragPrivacyStatuses = map[string]bool{"pending": true, "approved": true, "rejected": true, "all": true}
var ragLabelStatuses = map[string]bool{"draft": true, "confirmed": true, "rejected": true}
var ragFailureTypes = map[string]bool{
	"chunk_semantic_split": true, "embedding_margin_low": true,
	"candidate_evidence_missed": true, "reranker_evidence_dropped": true,
	"context_evidence_dropped": true, "context_truncated": true,
}

func (h *RagHandler) EvaluationTraces(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, "GET")
		return
	}
	workspaceID := r.URL.Query().Get("workspace_id")
	privacyStatus := r.URL.Query().Get("privacy_status")
	if privacyStatus == "" {
		privacyStatus = "pending"
	}
	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if _, err := fmt.Sscan(raw, &limit); err != nil {
			limit = 0
		}
	}
	if _, err := uuid.Parse(workspaceID); err != nil || !ragPrivacyStatuses[privacyStatus] || limit < 1 || limit > 100 {
		writeError(w, 400, "VALIDATION_ERROR", "RAG 审核队列参数无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 审核服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ListRagEvaluationTraces(ctx, workspaceID, privacyStatus, limit)
	if err != nil {
		h.writeMutationError(w, err, "RAG_REVIEW_LIST_FAILED", "RAG 审核队列读取失败")
		return
	}
	traces := make([]contracts.RagEvaluationTraceDTO, len(resp.Traces))
	for i, trace := range resp.Traces {
		traces[i] = ragEvaluationTraceFromCP(trace)
	}
	writeOK(w, contracts.ListRagEvaluationTracesOutput{Traces: traces})
}

func (h *RagHandler) EvaluationGates(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, "GET")
		return
	}
	limit := 20
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if _, err := fmt.Sscan(raw, &limit); err != nil {
			limit = 0
		}
	}
	if limit < 1 || limit > 100 {
		writeError(w, 400, "VALIDATION_ERROR", "RAG 门禁历史参数无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 门禁历史不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ListRagQualityGateRuns(ctx, limit)
	if err != nil {
		h.writeMutationError(w, err, "RAG_GATE_LIST_FAILED", "RAG 门禁历史读取失败")
		return
	}
	runs := make([]contracts.RagQualityGateRunDTO, len(resp.Runs))
	for i, value := range resp.Runs {
		runs[i] = contracts.RagQualityGateRunDTO{
			ID: contracts.ID(value.ID), GateID: value.GateID, CohortID: value.CohortID,
			BaselineID: value.BaselineID, Revision: value.Revision, Status: value.Status,
			SampleCount: value.SampleCount, Metrics: value.Metrics, Checks: value.Checks,
			GeneratedAt: value.GeneratedAt,
		}
	}
	metricTrends := make([]contracts.RagQualityMetricTrendDTO, len(resp.Insights.MetricTrends))
	for i, value := range resp.Insights.MetricTrends {
		metricTrends[i] = contracts.RagQualityMetricTrendDTO(value)
	}
	alerts := make([]contracts.RagQualityAlertDTO, len(resp.Insights.Alerts))
	for i, value := range resp.Insights.Alerts {
		alerts[i] = contracts.RagQualityAlertDTO(value)
	}
	clusters := make([]contracts.RagQualityFailureClusterDTO, len(resp.Insights.FailureClusters))
	for i, value := range resp.Insights.FailureClusters {
		clusters[i] = contracts.RagQualityFailureClusterDTO(value)
	}
	var previousRunID *contracts.ID
	if resp.Insights.PreviousRunID != nil {
		value := contracts.ID(*resp.Insights.PreviousRunID)
		previousRunID = &value
	}
	writeOK(w, contracts.ListRagQualityGateRunsOutput{
		Runs: runs,
		Insights: contracts.RagQualityGateInsightsDTO{
			ComparisonState:        resp.Insights.ComparisonState,
			CompatibleHistoryCount: resp.Insights.CompatibleHistoryCount,
			PreviousRunID:          previousRunID,
			MetricTrends:           metricTrends,
			Alerts:                 alerts,
			FailureClusters:        clusters,
		},
	})
}

func (h *RagHandler) EvaluationGateFailureTargets(w http.ResponseWriter, r *http.Request, runID string) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, "GET")
		return
	}
	failureType := r.URL.Query().Get("failure_type")
	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if _, err := fmt.Sscan(raw, &limit); err != nil {
			limit = 0
		}
	}
	if _, err := uuid.Parse(runID); err != nil || !ragFailureTypes[failureType] || limit < 1 || limit > 100 {
		writeError(w, 400, "VALIDATION_ERROR", "RAG 失败样本参数无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 失败样本服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ListRagQualityFailureTargets(ctx, runID, failureType, limit)
	if err != nil {
		h.writeMutationError(w, err, "RAG_FAILURE_TARGET_LIST_FAILED", "RAG 失败样本读取失败")
		return
	}
	targets := make([]contracts.RagQualityFailureTargetDTO, len(resp.Targets))
	for i, value := range resp.Targets {
		targets[i] = contracts.RagQualityFailureTargetDTO{
			CandidateID: value.CandidateID, TraceID: contracts.ID(value.TraceID), WorkspaceID: contracts.ID(value.WorkspaceID),
			QueryHash: value.QueryHash, FailureType: value.FailureType, SuspectedStage: value.SuspectedStage,
			Severity: value.Severity, MetricIDs: value.MetricIDs, PrivacyStatus: value.PrivacyStatus,
			LabelStatus: value.LabelStatus, LabelSource: value.LabelSource, ReviewState: value.ReviewState,
			Issue: ragQualityIssueFromCP(value.Issue),
		}
	}
	writeOK(w, contracts.ListRagQualityFailureTargetsOutput{Targets: targets})
}

func (h *RagHandler) EvaluationQualityIssue(w http.ResponseWriter, r *http.Request, issueID string) {
	if r.Method != http.MethodPatch {
		WriteMethodNotAllowed(w, "PATCH")
		return
	}
	if _, err := uuid.Parse(issueID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "issue_id 格式无效", "validation", false)
		return
	}
	var input contracts.UpdateRagQualityIssueInput
	if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&input); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "质量问题参数无效", "validation", false)
		return
	}
	owners := map[string]bool{"data_quality": true, "candidate_recall": true, "reranker": true, "context_assembly": true}
	statuses := map[string]bool{"open": true, "in_progress": true, "resolved": true, "dismissed": true}
	if input.ExpectedVersion < 1 || !owners[input.Owner] || !statuses[input.Status] || len(input.ResolutionNote) > 500 || ((input.Status == "resolved" || input.Status == "dismissed") && strings.TrimSpace(input.ResolutionNote) == "") {
		writeError(w, 400, "VALIDATION_ERROR", "质量问题参数无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "质量治理服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.UpdateRagQualityIssue(ctx, issueID, map[string]any{"expected_version": input.ExpectedVersion, "owner": input.Owner, "status": input.Status, "resolution_note": input.ResolutionNote})
	if err != nil {
		h.writeMutationError(w, err, "RAG_QUALITY_ISSUE_UPDATE_FAILED", "质量问题更新失败")
		return
	}
	writeOK(w, contracts.UpdateRagQualityIssueOutput{Issue: *ragQualityIssueFromCP(&resp.Issue)})
}

func (h *RagHandler) EvaluationQualityIssues(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, "GET")
		return
	}
	status, owner, failureType := r.URL.Query().Get("status"), r.URL.Query().Get("owner"), r.URL.Query().Get("failure_type")
	if status == "" {
		status = "all"
	}
	if owner == "" {
		owner = "all"
	}
	if failureType == "" {
		failureType = "all"
	}
	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if _, err := fmt.Sscan(raw, &limit); err != nil {
			limit = 0
		}
	}
	owners := map[string]bool{"all": true, "data_quality": true, "candidate_recall": true, "reranker": true, "context_assembly": true}
	statuses := map[string]bool{"all": true, "open": true, "in_progress": true, "resolved": true, "verified": true, "dismissed": true}
	if !statuses[status] || !owners[owner] || (failureType != "all" && !ragFailureTypes[failureType]) || limit < 1 || limit > 100 {
		writeError(w, 400, "VALIDATION_ERROR", "质量问题台账参数无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "质量问题台账服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ListRagQualityIssues(ctx, status, owner, failureType, limit)
	if err != nil {
		h.writeMutationError(w, err, "RAG_QUALITY_ISSUE_LIST_FAILED", "质量问题台账读取失败")
		return
	}
	issues := make([]contracts.RagQualityIssueLedgerItemDTO, len(resp.Issues))
	for i, value := range resp.Issues {
		issues[i] = contracts.RagQualityIssueLedgerItemDTO{
			Issue: *ragQualityIssueFromCP(&value.Issue), TraceID: contracts.ID(value.TraceID), WorkspaceID: contracts.ID(value.WorkspaceID),
			QueryHash: value.QueryHash, PrivacyStatus: value.PrivacyStatus, LabelStatus: value.LabelStatus,
			ReviewState: value.ReviewState, FirstSeenRevision: value.FirstSeenRevision,
			LastSeenRevision: value.LastSeenRevision, VerifiedRevision: value.VerifiedRevision,
		}
	}
	writeOK(w, contracts.ListRagQualityIssuesOutput{Issues: issues, Summary: contracts.RagQualityIssueSummaryDTO{
		Total: resp.Summary.Total, Open: resp.Summary.Open, InProgress: resp.Summary.InProgress,
		Resolved: resp.Summary.Resolved, Verified: resp.Summary.Verified, Dismissed: resp.Summary.Dismissed,
	}})
}

func ragQualityIssueFromCP(value *controlplane.RagQualityIssueDTO) *contracts.RagQualityIssueDTO {
	if value == nil {
		return nil
	}
	var verified *contracts.ID
	if value.VerifiedRunID != nil {
		id := contracts.ID(*value.VerifiedRunID)
		verified = &id
	}
	return &contracts.RagQualityIssueDTO{ID: contracts.ID(value.ID), CandidateID: value.CandidateID, TraceID: contracts.ID(value.TraceID), GateID: value.GateID, CohortID: value.CohortID, FailureType: value.FailureType, Owner: value.Owner, Status: value.Status, OccurrenceCount: value.OccurrenceCount, FirstSeenRunID: contracts.ID(value.FirstSeenRunID), LastSeenRunID: contracts.ID(value.LastSeenRunID), VerifiedRunID: verified, ResolutionNote: value.ResolutionNote, Version: value.Version, CreatedAt: value.CreatedAt, UpdatedAt: value.UpdatedAt}
}

func (h *RagHandler) EvaluationTraceItem(w http.ResponseWriter, r *http.Request, traceID, action string) {
	if _, err := uuid.Parse(traceID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "trace_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 审核服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	var resp *controlplane.RagEvaluationTraceDetailResponse
	var err error
	switch {
	case r.Method == http.MethodGet && action == "":
		workspaceID := r.URL.Query().Get("workspace_id")
		if _, parseErr := uuid.Parse(workspaceID); parseErr != nil {
			writeError(w, 400, "VALIDATION_ERROR", "workspace_id 格式无效", "validation", false)
			return
		}
		resp, err = h.controlPlane.InspectRagEvaluationTrace(ctx, traceID, workspaceID)
	case r.Method == http.MethodPost && action == "privacy":
		var input contracts.ReviewRagTracePrivacyInput
		if !decodeRagReview(w, r, &input) {
			return
		}
		if input.Decision != "approved" && input.Decision != "rejected" {
			writeError(w, 400, "VALIDATION_ERROR", "隐私复核决定无效", "validation", false)
			return
		}
		resp, err = h.controlPlane.ReviewRagEvaluationPrivacy(ctx, traceID, string(input.WorkspaceID), input.Decision)
	case r.Method == http.MethodPost && action == "label":
		var input contracts.ReviewRagTraceLabelInput
		if !decodeRagReview(w, r, &input) {
			return
		}
		if !ragLabelStatuses[input.Status] || len(input.PositiveChunkIDs) < 1 || len(input.PositiveChunkIDs) > 100 || len(input.HardNegativeChunkIDs) > 100 || len(input.Notes) > 500 || !validRagFeedbackChunkIDs(input.PositiveChunkIDs, input.HardNegativeChunkIDs) {
			writeError(w, 400, "VALIDATION_ERROR", "标签复核参数无效", "validation", false)
			return
		}
		resp, err = h.controlPlane.ReviewRagEvaluationLabel(ctx, traceID, string(input.WorkspaceID), input.Status, idsToStrings(input.PositiveChunkIDs), idsToStrings(input.HardNegativeChunkIDs), input.Notes)
	case r.Method == http.MethodPost && action == "promote":
		var input contracts.PromoteRagTraceInput
		if !decodeRagReview(w, r, &input) {
			return
		}
		resp, err = h.controlPlane.PromoteRagEvaluationTrace(ctx, traceID, string(input.WorkspaceID))
	default:
		WriteMethodNotAllowed(w, "GET, POST")
		return
	}
	if err != nil {
		h.writeMutationError(w, err, "RAG_REVIEW_FAILED", "RAG 审核操作失败")
		return
	}
	writeOK(w, ragEvaluationDetailFromCP(resp))
}

func decodeRagReview(w http.ResponseWriter, r *http.Request, target any) bool {
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16384)).Decode(target); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "RAG 审核请求无效", "validation", false)
		return false
	}
	var workspaceID string
	switch input := target.(type) {
	case *contracts.ReviewRagTracePrivacyInput:
		workspaceID = string(input.WorkspaceID)
	case *contracts.ReviewRagTraceLabelInput:
		workspaceID = string(input.WorkspaceID)
	case *contracts.PromoteRagTraceInput:
		workspaceID = string(input.WorkspaceID)
	}
	if _, err := uuid.Parse(workspaceID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "workspace_id 格式无效", "validation", false)
		return false
	}
	return true
}

func ragEvaluationTraceFromCP(value controlplane.RagEvaluationTraceDTO) contracts.RagEvaluationTraceDTO {
	return contracts.RagEvaluationTraceDTO{
		TraceID: contracts.ID(value.TraceID), WorkspaceID: contracts.ID(value.WorkspaceID), TaskID: contracts.ID(value.TaskID), RunID: contracts.ID(value.RunID),
		QueryHash: value.QueryHash, PrivacyStatus: value.PrivacyStatus, LabelStatus: value.LabelStatus, LabelSource: value.LabelSource,
		CandidateCount: value.CandidateCount, RerankedCount: value.RerankedCount, ContextChunkCount: value.ContextChunkCount, ContextTruncated: value.ContextTruncated,
		PipelineVersions: value.PipelineVersions, CreatedAt: value.CreatedAt,
	}
}

func ragEvaluationDetailFromCP(value *controlplane.RagEvaluationTraceDetailResponse) contracts.RagEvaluationTraceDetailOutput {
	evidence := make([]contracts.RagFeedbackEvidenceDTO, len(value.Evidence))
	for i, item := range value.Evidence {
		evidence[i] = contracts.RagFeedbackEvidenceDTO{ChunkID: contracts.ID(item.ChunkID), DocumentID: contracts.ID(item.DocumentID), ContentHash: item.ContentHash, CandidateRank: item.CandidateRank, RerankedRank: item.RerankedRank, InContext: item.InContext, Sources: item.Sources, Snippet: item.Snippet}
	}
	var label *contracts.RagEvaluationLabelDTO
	if value.Label != nil {
		label = &contracts.RagEvaluationLabelDTO{ID: contracts.ID(value.Label.ID), Source: value.Label.Source, Status: value.Label.Status, PositiveChunkIDs: stringsToIDs(value.Label.PositiveChunkIDs), HardNegativeChunkIDs: stringsToIDs(value.Label.HardNegativeChunkIDs), Notes: value.Label.Notes}
	}
	var candidate *contracts.RagPromotionCandidateDTO
	if value.PromotionCandidate != nil {
		candidate = &contracts.RagPromotionCandidateDTO{SchemaVersion: value.PromotionCandidate.SchemaVersion, TraceID: contracts.ID(value.PromotionCandidate.TraceID), QueryHash: value.PromotionCandidate.QueryHash, RawQueryIncluded: value.PromotionCandidate.RawQueryIncluded, RawChunkContentIncluded: value.PromotionCandidate.RawChunkContentIncluded}
	}
	return contracts.RagEvaluationTraceDetailOutput{Trace: ragEvaluationTraceFromCP(value.Trace), Query: value.Query, Request: value.Request, Evidence: evidence, Label: label, PromotionCandidate: candidate}
}

func (h *RagHandler) Item(w http.ResponseWriter, r *http.Request, documentID, action string) {
	if _, err := uuid.Parse(documentID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "document_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 文档服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	switch {
	case r.Method == http.MethodPost && action == "restart":
		var input contracts.RestartRagDocumentInput
		if !decodeRagMutation(w, r, &input) {
			return
		}
		resp, err := h.controlPlane.RestartRagDocument(
			ctx, input.WorkspaceID, documentID, input.ExpectedVersion,
		)
		if err != nil {
			h.writeMutationError(w, err, "RAG_RESTART_FAILED", "RAG 作业重新执行失败")
			return
		}
		writeOK(w, contracts.RestartRagDocumentOutput{
			DocumentID: resp.DocumentID,
			JobID:      resp.JobID,
			Status:     resp.Status,
		})
	case r.Method == http.MethodPatch && action == "":
		var input contracts.UpdateRagDocumentInput
		if !decodeRagMutation(w, r, &input) {
			return
		}
		resp, err := h.controlPlane.UpdateRagDocument(
			ctx, input.WorkspaceID, documentID, input.ExpectedVersion, input.Enabled,
		)
		if err != nil {
			h.writeMutationError(w, err, "RAG_UPDATE_FAILED", "RAG 文档状态更新失败")
			return
		}
		writeOK(w, contracts.UpdateRagDocumentOutput{
			DocumentID: resp.DocumentID, Status: resp.Status, Version: resp.Version,
		})
	case r.Method == http.MethodPost && action == "cancel":
		var input contracts.CancelRagDocumentInput
		if !decodeRagMutation(w, r, &input) {
			return
		}
		resp, err := h.controlPlane.CancelRagDocument(
			ctx, input.WorkspaceID, documentID, input.ExpectedVersion,
		)
		if err != nil {
			h.writeMutationError(w, err, "RAG_CANCEL_FAILED", "RAG 作业取消失败")
			return
		}
		writeOK(w, contracts.CancelRagDocumentOutput{
			DocumentID: resp.DocumentID, Status: resp.Status, Version: resp.Version,
			JobID: resp.JobID, JobStatus: resp.JobStatus,
		})
	case r.Method == http.MethodPost && action == "delete-requests":
		var input contracts.CreateRagDeleteRequestInput
		if !decodeRagMutation(w, r, &input) {
			return
		}
		resp, err := h.controlPlane.CreateRagDeleteRequest(ctx, input.WorkspaceID, documentID, input.ExpectedVersion)
		if err != nil {
			h.writeMutationError(w, err, "RAG_DELETE_REQUEST_FAILED", "创建删除确认失败")
			return
		}
		writeOK(w, ragPermissionFromCP(*resp))
	default:
		WriteMethodNotAllowed(w, "PATCH, POST")
	}
}

func decodeRagMutation(w http.ResponseWriter, r *http.Request, target any) bool {
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(target); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "RAG 文档操作请求无效", "validation", false)
		return false
	}
	var workspaceID string
	var expectedVersion int
	switch input := target.(type) {
	case *contracts.RestartRagDocumentInput:
		workspaceID, expectedVersion = input.WorkspaceID, input.ExpectedVersion
	case *contracts.UpdateRagDocumentInput:
		workspaceID, expectedVersion = input.WorkspaceID, input.ExpectedVersion
	case *contracts.CancelRagDocumentInput:
		workspaceID, expectedVersion = input.WorkspaceID, input.ExpectedVersion
	case *contracts.CreateRagDeleteRequestInput:
		workspaceID, expectedVersion = input.WorkspaceID, input.ExpectedVersion
	}
	if _, err := uuid.Parse(workspaceID); err != nil || expectedVersion < 1 {
		writeError(w, 400, "VALIDATION_ERROR", "workspace_id 或 expected_version 无效", "validation", false)
		return false
	}
	return true
}

func (h *RagHandler) ResolveDelete(w http.ResponseWriter, r *http.Request, requestID string) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, "POST")
		return
	}
	if _, err := uuid.Parse(requestID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "request_id 格式无效", "validation", false)
		return
	}
	var input contracts.ResolveRagDeleteRequestInput
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&input); err != nil || (input.Decision != "allow_once" && input.Decision != "deny") {
		writeError(w, 400, "VALIDATION_ERROR", "删除决定无效", "validation", false)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ResolveRagDeleteRequest(ctx, requestID, input.Decision, input.Note)
	if err != nil {
		h.writeMutationError(w, err, "RAG_DELETE_FAILED", "永久删除失败")
		return
	}
	writeOK(w, contracts.RagDeleteResolutionOutput{
		Permission: ragPermissionFromCP(resp.Permission), DocumentID: resp.DocumentID,
		Deleted: resp.Deleted, CleanupPendingCount: resp.CleanupPendingCount,
		SourceArtifactRetained: resp.SourceArtifactRetained,
	})
}

func ragPermissionFromCP(request controlplane.PermissionRequestDTO) contracts.PermissionRequestDTO {
	return contracts.PermissionRequestDTO{
		ID: request.ID, TaskID: request.TaskID, RunID: request.RunID, StepID: request.StepID,
		ToolName: request.ToolName, ActionSummary: request.ActionSummary, Reason: request.Reason,
		RiskLevel: request.RiskLevel, Scope: contracts.PermissionScopeDTO{Type: fmt.Sprint(request.Scope["type"])},
		ArgumentsSummary: request.ArgumentsSummary, AllowedDecisions: request.AllowedDecisions,
		CreatedAt: request.CreatedAt, ExpiresAt: request.ExpiresAt,
		Status: request.Status, Decision: request.Decision,
	}
}

func (h *RagHandler) writeMutationError(w http.ResponseWriter, err error, code, message string) {
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, message, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, message, category, recoverable)
			return
		}
		writeError(w, 503, code, message, "storage", true)
	}
}

type RagHandler struct{ controlPlane RagControlPlane }

func NewRagHandler(cp RagControlPlane) *RagHandler { return &RagHandler{controlPlane: cp} }

func (h *RagHandler) Documents(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodPost {
		h.Upload(w, r)
		return
	}
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, "GET, POST")
		return
	}
	workspaceID := r.URL.Query().Get("workspace_id")
	if _, err := uuid.Parse(workspaceID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "workspace_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 文档服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ListRagDocuments(ctx, workspaceID, r.URL.Query().Get("include_disabled") == "true")
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, message, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, message, category, recoverable)
			return
		}
		writeError(w, 503, "RAG_SERVICE_ERROR", "RAG 文档服务暂不可用", "storage", true)
		return
	}
	documents := make([]contracts.RagDocumentDTO, len(resp.Documents))
	for i, document := range resp.Documents {
		documents[i] = ragDocumentFromCP(document)
	}
	writeOK(w, contracts.ListRagDocumentsOutput{Documents: documents})
}

const maxRagUploadBytes int64 = 50 * 1024 * 1024

func (h *RagHandler) Upload(w http.ResponseWriter, r *http.Request) {
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 上传服务不可用", "internal", true)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxRagUploadBytes+1024*1024)
	if err := r.ParseMultipartForm(maxRagUploadBytes + 1024*1024); err != nil {
		writeError(w, 400, "RAG_UPLOAD_INVALID", "上传表单无效或文件过大", "validation", false)
		return
	}
	workspaceID := strings.TrimSpace(r.FormValue("workspace_id"))
	if _, err := uuid.Parse(workspaceID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "workspace_id 格式无效", "validation", false)
		return
	}
	permissionRequestID := strings.TrimSpace(r.FormValue("permission_request_id"))
	if _, err := uuid.Parse(permissionRequestID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "permission_request_id 格式无效", "validation", false)
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeError(w, 400, "RAG_UPLOAD_FILE_REQUIRED", "请选择 PDF 文件", "validation", false)
		return
	}
	defer file.Close()
	filename := filepath.Base(strings.TrimSpace(header.Filename))
	if filename == "." || filename == "" || !strings.EqualFold(filepath.Ext(filename), ".pdf") {
		writeError(w, 400, "RAG_UPLOAD_FILENAME_INVALID", "只支持 PDF 文件", "validation", false)
		return
	}
	content, err := io.ReadAll(io.LimitReader(file, maxRagUploadBytes+1))
	if err != nil || len(content) == 0 || int64(len(content)) > maxRagUploadBytes {
		writeError(w, 400, "RAG_UPLOAD_SIZE_INVALID", "PDF 必须大于 0 且不超过 50 MiB", "validation", false)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	resp, err := h.controlPlane.UploadRagDocument(ctx, workspaceID, permissionRequestID, filename, content)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, message, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, message, category, recoverable)
			return
		}
		writeError(w, 503, "RAG_UPLOAD_FAILED", "RAG 文档上传失败", "storage", true)
		return
	}
	writeOK(w, contracts.UploadRagDocumentOutput{
		ArtifactID: resp.ArtifactID, DocumentID: resp.DocumentID, JobID: resp.JobID,
		Status: resp.Status, Uploaded: resp.Uploaded, Created: resp.Created,
	})
}

func (h *RagHandler) UploadRequests(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, "POST")
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "RAG 上传权限服务不可用", "internal", true)
		return
	}
	var input contracts.CreateRagUploadRequestInput
	if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&input); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "RAG 上传权限参数无效", "validation", false)
		return
	}
	filename := filepath.Base(strings.TrimSpace(input.Filename))
	if _, err := uuid.Parse(input.WorkspaceID); err != nil ||
		filename == "." || filename == "" || !strings.EqualFold(filepath.Ext(filename), ".pdf") ||
		input.SizeBytes < 1 || input.SizeBytes > maxRagUploadBytes ||
		len(input.ContentSHA256) != 64 {
		writeError(w, 400, "VALIDATION_ERROR", "RAG 上传权限参数无效", "validation", false)
		return
	}
	for _, value := range input.ContentSHA256 {
		if !strings.ContainsRune("0123456789abcdef", value) {
			writeError(w, 400, "VALIDATION_ERROR", "content_sha256 格式无效", "validation", false)
			return
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	request, err := h.controlPlane.CreateRagUploadRequest(
		ctx, input.WorkspaceID, filename, input.SizeBytes, input.ContentSHA256,
	)
	if err != nil {
		h.writeMutationError(w, err, "RAG_UPLOAD_PERMISSION_FAILED", "创建 RAG 上传确认失败")
		return
	}
	writeOK(w, ragPermissionFromCP(*request))
}

func (h *RagHandler) ResolveUploadRequest(w http.ResponseWriter, r *http.Request, requestID string) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, "POST")
		return
	}
	if _, err := uuid.Parse(requestID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "request_id 格式无效", "validation", false)
		return
	}
	var input contracts.ResolveRagUploadRequestInput
	if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&input); err != nil ||
		(input.Decision != "allow_once" && input.Decision != "deny") || len(input.Note) > 500 {
		writeError(w, 400, "VALIDATION_ERROR", "RAG 上传权限决定无效", "validation", false)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	request, err := h.controlPlane.ResolveRagUploadRequest(ctx, requestID, input.Decision, input.Note)
	if err != nil {
		h.writeMutationError(w, err, "RAG_UPLOAD_PERMISSION_RESOLVE_FAILED", "处理 RAG 上传确认失败")
		return
	}
	writeOK(w, ragPermissionFromCP(*request))
}

func ragDocumentFromCP(document controlplane.RagDocumentDTO) contracts.RagDocumentDTO {
	var latestJob *contracts.RagIngestionJobDTO
	if document.LatestJob != nil {
		job := document.LatestJob
		latestJob = &contracts.RagIngestionJobDTO{
			ID: job.ID, Status: job.Status, Attempts: job.Attempts, MaxAttempts: job.MaxAttempts,
			EmbeddingAttempts: job.EmbeddingAttempts, EmbeddingMaxAttempts: job.EmbeddingMaxAttempts,
			Progress: contracts.RagJobProgressDTO{
				ActiveExecutor: job.Progress.ActiveExecutor, PageCount: job.Progress.PageCount,
				NativeExtractionDone: job.Progress.NativeExtractionDone,
				VisualPagesTotal:     job.Progress.VisualPagesTotal,
				VisualPagesCompleted: job.Progress.VisualPagesCompleted,
				VisualRouteCounts:    job.Progress.VisualRouteCounts,
				ChunksTotal:          job.Progress.ChunksTotal, EmbeddingTotal: job.Progress.EmbeddingTotal,
				EmbeddingCompleted: job.Progress.EmbeddingCompleted,
			},
			ErrorCode: job.ErrorCode, NextRetryAt: job.NextRetryAt, StartedAt: job.StartedAt,
			CompletedAt: job.CompletedAt, FailedAt: job.FailedAt, CreatedAt: job.CreatedAt, UpdatedAt: job.UpdatedAt,
		}
	}
	return contracts.RagDocumentDTO{
		ID: document.ID, WorkspaceID: document.WorkspaceID, SourceArtifactID: document.SourceArtifactID,
		Title: document.Title, MimeType: document.MimeType, Status: document.Status,
		IngestionPolicyVersion: document.IngestionPolicyVersion, ParserVersion: document.ParserVersion,
		ChunkerVersion: document.ChunkerVersion, EmbeddingProvider: document.EmbeddingProvider,
		EmbeddingModel: document.EmbeddingModel, EmbeddingDimensions: document.EmbeddingDimensions,
		ChunkCount: document.ChunkCount, IndexedAt: document.IndexedAt, Version: document.Version,
		CreatedAt: document.CreatedAt, UpdatedAt: document.UpdatedAt, LatestJob: latestJob,
		IndexState: document.IndexState, IndexStaleReasons: document.IndexStaleReasons,
		IndexTarget: document.IndexTarget,
	}
}
