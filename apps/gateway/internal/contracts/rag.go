package contracts

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

type ListRagDocumentsOutput struct {
	Documents []RagDocumentDTO `json:"documents"`
}

type RagFeedbackDTO struct {
	ID               ID                `json:"id"`
	TraceID          ID                `json:"trace_id"`
	WorkspaceID      ID                `json:"workspace_id"`
	TaskID           ID                `json:"task_id"`
	RunID            ID                `json:"run_id"`
	MessageID        ID                `json:"message_id"`
	Kind             string            `json:"kind"`
	CitationChunkID  ID                `json:"citation_chunk_id,omitempty"`
	Status           string            `json:"status"`
	FailureCategory  string            `json:"failure_category,omitempty"`
	QueryHash        string            `json:"query_hash,omitempty"`
	PipelineVersions map[string]string `json:"pipeline_versions,omitempty"`
	ResultCount      int               `json:"result_count,omitempty"`
	ContextTruncated bool              `json:"context_truncated,omitempty"`
	CreatedAt        string            `json:"created_at"`
	UpdatedAt        string            `json:"updated_at"`
}

type SubmitRagFeedbackInput struct {
	MessageID       ID     `json:"message_id"`
	Kind            string `json:"kind"`
	CitationChunkID ID     `json:"citation_chunk_id,omitempty"`
}

type RagFeedbackMutationOutput struct {
	Feedback RagFeedbackDTO `json:"feedback"`
}
type ListRagFeedbackOutput struct {
	Feedback []RagFeedbackDTO `json:"feedback"`
}
type ResolveRagFeedbackInput struct {
	Status string `json:"status"`
}
type RagFeedbackEvidenceDTO struct {
	ChunkID       ID       `json:"chunk_id"`
	DocumentID    ID       `json:"document_id"`
	ContentHash   string   `json:"content_hash"`
	CandidateRank *int     `json:"candidate_rank"`
	RerankedRank  *int     `json:"reranked_rank"`
	InContext     bool     `json:"in_context"`
	Sources       []string `json:"sources"`
	Snippet       *string  `json:"snippet"`
}
type RagFeedbackLabelDTO struct {
	ID                   ID     `json:"id"`
	Source               string `json:"source"`
	Status               string `json:"status"`
	PositiveChunkIDs     []ID   `json:"positive_chunk_ids"`
	HardNegativeChunkIDs []ID   `json:"hard_negative_chunk_ids"`
}
type RagFeedbackDetailOutput struct {
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
type TriageRagFeedbackInput struct {
	FailureCategory      string `json:"failure_category"`
	PositiveChunkIDs     []ID   `json:"positive_chunk_ids"`
	HardNegativeChunkIDs []ID   `json:"hard_negative_chunk_ids"`
}
type TriageRagFeedbackOutput struct {
	Feedback    RagFeedbackDTO `json:"feedback"`
	LabelStatus string         `json:"label_status,omitempty"`
}
type RagEvaluationTraceDTO struct {
	TraceID           ID                `json:"trace_id"`
	WorkspaceID       ID                `json:"workspace_id"`
	TaskID            ID                `json:"task_id"`
	RunID             ID                `json:"run_id"`
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
type ListRagEvaluationTracesOutput struct {
	Traces []RagEvaluationTraceDTO `json:"traces"`
}
type RagQualityGateRunDTO struct {
	ID          ID                 `json:"id"`
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
	PreviousRunID          *ID                           `json:"previous_run_id"`
	MetricTrends           []RagQualityMetricTrendDTO    `json:"metric_trends"`
	Alerts                 []RagQualityAlertDTO          `json:"alerts"`
	FailureClusters        []RagQualityFailureClusterDTO `json:"failure_clusters"`
}
type ListRagQualityGateRunsOutput struct {
	Runs     []RagQualityGateRunDTO    `json:"runs"`
	Insights RagQualityGateInsightsDTO `json:"insights"`
}
type RagQualityFailureTargetDTO struct {
	CandidateID    string              `json:"candidate_id"`
	TraceID        ID                  `json:"trace_id"`
	WorkspaceID    ID                  `json:"workspace_id"`
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
type ListRagQualityFailureTargetsOutput struct {
	Targets []RagQualityFailureTargetDTO `json:"targets"`
}
type RagQualityIssueDTO struct {
	ID              ID     `json:"id"`
	CandidateID     string `json:"candidate_id"`
	TraceID         ID     `json:"trace_id"`
	GateID          string `json:"gate_id"`
	CohortID        string `json:"cohort_id"`
	FailureType     string `json:"failure_type"`
	Owner           string `json:"owner"`
	Status          string `json:"status"`
	OccurrenceCount int    `json:"occurrence_count"`
	FirstSeenRunID  ID     `json:"first_seen_run_id"`
	LastSeenRunID   ID     `json:"last_seen_run_id"`
	VerifiedRunID   *ID    `json:"verified_run_id"`
	ResolutionNote  string `json:"resolution_note"`
	Version         int    `json:"version"`
	CreatedAt       string `json:"created_at"`
	UpdatedAt       string `json:"updated_at"`
}
type UpdateRagQualityIssueInput struct {
	ExpectedVersion int    `json:"expected_version"`
	Owner           string `json:"owner"`
	Status          string `json:"status"`
	ResolutionNote  string `json:"resolution_note"`
}
type UpdateRagQualityIssueOutput struct {
	Issue RagQualityIssueDTO `json:"issue"`
}
type RagQualityIssueLedgerItemDTO struct {
	Issue             RagQualityIssueDTO `json:"issue"`
	TraceID           ID                 `json:"trace_id"`
	WorkspaceID       ID                 `json:"workspace_id"`
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
type ListRagQualityIssuesOutput struct {
	Issues  []RagQualityIssueLedgerItemDTO `json:"issues"`
	Summary RagQualityIssueSummaryDTO      `json:"summary"`
}
type RagEvaluationLabelDTO struct {
	ID                   ID     `json:"id"`
	Source               string `json:"source"`
	Status               string `json:"status"`
	PositiveChunkIDs     []ID   `json:"positive_chunk_ids"`
	HardNegativeChunkIDs []ID   `json:"hard_negative_chunk_ids"`
	Notes                string `json:"notes"`
}
type RagPromotionCandidateDTO struct {
	SchemaVersion           int    `json:"schema_version"`
	TraceID                 ID     `json:"trace_id"`
	QueryHash               string `json:"query_hash"`
	RawQueryIncluded        bool   `json:"raw_query_included"`
	RawChunkContentIncluded bool   `json:"raw_chunk_content_included"`
}
type RagEvaluationTraceDetailOutput struct {
	Trace              RagEvaluationTraceDTO     `json:"trace"`
	Query              *string                   `json:"query"`
	Request            map[string]any            `json:"request"`
	Evidence           []RagFeedbackEvidenceDTO  `json:"evidence"`
	Label              *RagEvaluationLabelDTO    `json:"label"`
	PromotionCandidate *RagPromotionCandidateDTO `json:"promotion_candidate"`
}
type ReviewRagTracePrivacyInput struct {
	WorkspaceID ID     `json:"workspace_id"`
	Decision    string `json:"decision"`
}
type ReviewRagTraceLabelInput struct {
	WorkspaceID          ID     `json:"workspace_id"`
	Status               string `json:"status"`
	PositiveChunkIDs     []ID   `json:"positive_chunk_ids"`
	HardNegativeChunkIDs []ID   `json:"hard_negative_chunk_ids"`
	Notes                string `json:"notes"`
}
type PromoteRagTraceInput struct {
	WorkspaceID ID `json:"workspace_id"`
}

type UploadRagDocumentOutput struct {
	ArtifactID string `json:"artifact_id"`
	DocumentID string `json:"document_id"`
	JobID      string `json:"job_id"`
	Status     string `json:"status"`
	Uploaded   bool   `json:"uploaded"`
	Created    bool   `json:"created"`
}

type CreateRagUploadRequestInput struct {
	WorkspaceID   string `json:"workspace_id"`
	Filename      string `json:"filename"`
	SizeBytes     int64  `json:"size_bytes"`
	ContentSHA256 string `json:"content_sha256"`
}

type ResolveRagUploadRequestInput struct {
	Decision string `json:"decision"`
	Note     string `json:"note,omitempty"`
}

type RestartRagDocumentInput struct {
	WorkspaceID     string `json:"workspace_id"`
	ExpectedVersion int    `json:"expected_version"`
}

type UpdateRagDocumentInput struct {
	WorkspaceID     string `json:"workspace_id"`
	ExpectedVersion int    `json:"expected_version"`
	Enabled         bool   `json:"enabled"`
}

type UpdateRagDocumentOutput struct {
	DocumentID string `json:"document_id"`
	Status     string `json:"status"`
	Version    int    `json:"version"`
}

type CancelRagDocumentInput struct {
	WorkspaceID     string `json:"workspace_id"`
	ExpectedVersion int    `json:"expected_version"`
}

type CancelRagDocumentOutput struct {
	DocumentID string `json:"document_id"`
	Status     string `json:"status"`
	Version    int    `json:"version"`
	JobID      string `json:"job_id"`
	JobStatus  string `json:"job_status"`
}

type RestartRagDocumentOutput struct {
	DocumentID string `json:"document_id"`
	JobID      string `json:"job_id"`
	Status     string `json:"status"`
}
type CreateRagDeleteRequestInput struct {
	WorkspaceID     string `json:"workspace_id"`
	ExpectedVersion int    `json:"expected_version"`
}
type ResolveRagDeleteRequestInput struct {
	Decision string `json:"decision"`
	Note     string `json:"note,omitempty"`
}
type RagDeleteResolutionOutput struct {
	Permission             PermissionRequestDTO `json:"permission"`
	DocumentID             string               `json:"document_id"`
	Deleted                bool                 `json:"deleted"`
	CleanupPendingCount    int                  `json:"cleanup_pending_count"`
	SourceArtifactRetained bool                 `json:"source_artifact_retained"`
}
