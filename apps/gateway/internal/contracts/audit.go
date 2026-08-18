package contracts

import (
	"time"
)

// -- Audit Log --

// AuditLogDTO 是安全审计投影；details_summary 已由 Python Application 脱敏并限长。
type AuditLogDTO struct {
	ID                 ID                     `json:"id"`
	EventType          string                 `json:"event_type"`
	Actor              string                 `json:"actor"`
	ActionSummary      string                 `json:"action_summary"`
	TaskID             ID                     `json:"task_id,omitempty"`
	RunID              ID                     `json:"run_id,omitempty"`
	StepID             ID                     `json:"step_id,omitempty"`
	ToolCallID         ID                     `json:"tool_call_id,omitempty"`
	RiskLevel          RiskLevel              `json:"risk_level,omitempty"`
	PermissionDecision string                 `json:"permission_decision,omitempty"`
	ResultSummary      string                 `json:"result_summary,omitempty"`
	ErrorCode          string                 `json:"error_code,omitempty"`
	DetailsSummary     map[string]interface{} `json:"details_summary"`
	CreatedAt          string                 `json:"created_at"`
}

type ListAuditLogsOutput struct {
	AuditLogs  []AuditLogDTO `json:"audit_logs"`
	NextCursor *string       `json:"next_cursor,omitempty"`
}

type AuditRetentionPreviewDTO struct {
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

type CreateAuditRetentionRequestInput struct {
	StandardDays  int `json:"standard_days"`
	ExtendedDays  int `json:"extended_days"`
	MaxScan       int `json:"max_scan"`
	MaxCandidates int `json:"max_candidates"`
}

type CreateAuditRetentionRequestOutput struct {
	Request PermissionRequestDTO `json:"request"`
}

type ResolveAuditRetentionRequestInput struct {
	Decision string `json:"decision"`
	Note     string `json:"note,omitempty"`
}

type AuditRetentionResolutionDTO struct {
	Permission     PermissionRequestDTO `json:"permission"`
	DeletedRecords int                  `json:"deleted_records"`
	HasMore        bool                 `json:"has_more"`
}

func NowISO() string {
	return time.Now().UTC().Format(time.RFC3339)
}
