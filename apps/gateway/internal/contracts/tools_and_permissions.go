package contracts

// -- Permission --

type PermissionScopeDTO struct {
	Type          string `json:"type"`
	Resource      string `json:"resource,omitempty"`
	WorkspacePath string `json:"workspace_path,omitempty"`
	Path          string `json:"path,omitempty"`
	ToolName      string `json:"tool_name,omitempty"`
	McpServerID   string `json:"mcp_server_id,omitempty"`
}

type PermissionDecisionType = string

type PermissionRequestDTO struct {
	ID               ID                       `json:"id"`
	TaskID           ID                       `json:"task_id"`
	RunID            ID                       `json:"run_id"`
	StepID           ID                       `json:"step_id,omitempty"`
	ToolName         string                   `json:"tool_name"`
	ActionSummary    string                   `json:"action_summary"`
	Reason           string                   `json:"reason,omitempty"`
	RiskLevel        RiskLevel                `json:"risk_level"`
	Scope            PermissionScopeDTO       `json:"scope"`
	ArgumentsSummary map[string]interface{}   `json:"arguments_summary"`
	AllowedDecisions []PermissionDecisionType `json:"allowed_decisions"`
	CreatedAt        string                   `json:"created_at"`
	ExpiresAt        string                   `json:"expires_at"`
	Status           string                   `json:"status,omitempty"`
	Decision         string                   `json:"decision,omitempty"`
}

type PermissionDecisionDTO struct {
	RequestID ID     `json:"request_id"`
	Decision  string `json:"decision"`
	Note      string `json:"note,omitempty"`
}

// -- ToolCall --

type ToolPermissionStatus string

const (
	ToolPermissionNotRequired ToolPermissionStatus = "not_required"
	ToolPermissionPending     ToolPermissionStatus = "pending"
	ToolPermissionApproved    ToolPermissionStatus = "approved"
	ToolPermissionDenied      ToolPermissionStatus = "denied"
	ToolPermissionExpired     ToolPermissionStatus = "expired"
)

type ToolCallDTO struct {
	ID               ID                     `json:"id"`
	RunID            ID                     `json:"run_id"`
	StepID           ID                     `json:"step_id"`
	ToolName         string                 `json:"tool_name"`
	Provider         string                 `json:"provider"`
	McpServerID      string                 `json:"mcp_server_id,omitempty"`
	RiskLevel        RiskLevel              `json:"risk_level"`
	Arguments        map[string]interface{} `json:"arguments"`
	Result           *ToolResultDTO         `json:"result,omitempty"`
	PermissionReqID  ID                     `json:"permission_request_id,omitempty"`
	PermissionStatus ToolPermissionStatus   `json:"permission_status"`
	Status           string                 `json:"status"`
	Error            *AppError              `json:"error,omitempty"`
	StartedAt        string                 `json:"started_at,omitempty"`
	CompletedAt      string                 `json:"completed_at,omitempty"`
	DurationMs       int64                  `json:"duration_ms,omitempty"`
}

type ToolResultDTO struct {
	Kind         string               `json:"kind"`
	Summary      string               `json:"summary"`
	Data         interface{}          `json:"data,omitempty"`
	ArtifactIDs  []ID                 `json:"artifact_ids,omitempty"`
	Deliverables []ToolDeliverableDTO `json:"deliverables,omitempty"`
}

type ToolDeliverableDTO struct {
	Kind        string `json:"kind"`
	Title       string `json:"title"`
	Path        string `json:"path"`
	SizeBytes   int64  `json:"size_bytes"`
	MimeType    string `json:"mime_type"`
	ContentHash string `json:"content_hash"`
}

// -- Artifact --

type ArtifactProducerDTO struct {
	Type       string `json:"type"`
	ToolCallID ID     `json:"tool_call_id,omitempty"`
}

type ArtifactDTO struct {
	ID            ID                     `json:"id"`
	TaskID        ID                     `json:"task_id"`
	RunID         ID                     `json:"run_id"`
	Kind          string                 `json:"kind"`
	Title         string                 `json:"title"`
	Purpose       string                 `json:"purpose"`
	Producer      ArtifactProducerDTO    `json:"producer"`
	Content       string                 `json:"content,omitempty"`
	FileSizeBytes int64                  `json:"file_size_bytes,omitempty"`
	MimeType      string                 `json:"mime_type,omitempty"`
	ContentHash   string                 `json:"content_hash,omitempty"`
	FilePath      string                 `json:"file_path,omitempty"`
	Metadata      map[string]interface{} `json:"metadata,omitempty"`
	CreatedAt     string                 `json:"created_at"`
}

// -- RuntimeEvent --

type RuntimeEventType = string

type RuntimeEvent struct {
	ID        ID                     `json:"id"`
	Type      RuntimeEventType       `json:"type"`
	TaskID    ID                     `json:"task_id,omitempty"`
	RunID     ID                     `json:"run_id,omitempty"`
	StepID    ID                     `json:"step_id,omitempty"`
	Sequence  int64                  `json:"sequence,omitempty"`
	Timestamp string                 `json:"timestamp"`
	Payload   map[string]interface{} `json:"payload"`
}
