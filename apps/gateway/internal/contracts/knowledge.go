package contracts

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

type ListKnowledgeVaultsOutput struct {
	Vaults        []KnowledgeVaultDTO `json:"vaults"`
	SuggestedPath string              `json:"suggested_path"`
}
type ConnectKnowledgeVaultInput struct {
	Path string `json:"path"`
}
type KnowledgeVaultMutationOutput struct {
	Vault KnowledgeVaultDTO `json:"vault"`
}
type ListKnowledgeDocumentsOutput struct {
	Documents []KnowledgeDocumentDTO `json:"documents"`
}
type CreateKnowledgeDocumentInput struct {
	Title      string   `json:"title"`
	Kind       string   `json:"kind"`
	Content    string   `json:"content"`
	Tags       []string `json:"tags"`
	SourceURLs []string `json:"source_urls"`
}
type KnowledgeDocumentMutationOutput struct {
	Document KnowledgeDocumentDTO `json:"document"`
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
type ListScheduledTasksOutput struct {
	ScheduledTasks []ScheduledTaskDTO `json:"scheduled_tasks"`
}
type CreateScheduledTaskInput struct {
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
type UpdateScheduledTaskInput struct {
	ExpectedVersion int    `json:"expected_version"`
	Status          string `json:"status"`
}
type ScheduledTaskMutationOutput struct {
	ScheduledTask ScheduledTaskDTO `json:"scheduled_task"`
}
type ScheduledExecutionOutput struct {
	Execution ScheduledExecutionDTO `json:"execution"`
}
