package contracts

// -- Task --

type TaskStatus = string

type TaskDTO struct {
	ID              ID         `json:"id"`
	ConversationID  ID         `json:"conversation_id"`
	Title           string     `json:"title"`
	UserGoal        string     `json:"user_goal"`
	Status          TaskStatus `json:"status"`
	WorkspacePath   string     `json:"workspace_path,omitempty"`
	WorkspaceID     ID         `json:"workspace_id,omitempty"`
	ActiveRunID     ID         `json:"active_run_id,omitempty"`
	LastStepSummary string     `json:"last_step_summary,omitempty"`
	RiskLevel       RiskLevel  `json:"risk_level,omitempty"`
	CreatedAt       string     `json:"created_at"`
	UpdatedAt       string     `json:"updated_at"`
}

// -- AgentRun --

type AgentRunStatus = string

type AgentRunDTO struct {
	ID            ID             `json:"id"`
	TaskID        ID             `json:"task_id"`
	AgentID       ID             `json:"agent_id"`
	Mode          string         `json:"mode"`
	Status        AgentRunStatus `json:"status"`
	CurrentStepID ID             `json:"current_step_id,omitempty"`
	FinalOutput   *ArtifactDTO   `json:"final_output,omitempty"`
	CreatedAt     string         `json:"created_at"`
	UpdatedAt     string         `json:"updated_at"`
}

// -- ExecutionStep --

type StepType = string
type StepStatus = string

type ExecutionStepDTO struct {
	ID           ID                     `json:"id"`
	RunID        ID                     `json:"run_id"`
	ParentStepID ID                     `json:"parent_step_id,omitempty"`
	Type         StepType               `json:"type"`
	Status       StepStatus             `json:"status"`
	Title        string                 `json:"title"`
	Summary      string                 `json:"summary,omitempty"`
	Input        map[string]interface{} `json:"input,omitempty"`
	Output       map[string]interface{} `json:"output,omitempty"`
	Error        *AppError              `json:"error,omitempty"`
	StartedAt    string                 `json:"started_at,omitempty"`
	CompletedAt  string                 `json:"completed_at,omitempty"`
	DurationMs   int64                  `json:"duration_ms,omitempty"`
}
