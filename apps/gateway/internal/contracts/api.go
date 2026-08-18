package contracts

// -- API Input / Output --

type CreateTaskInput struct {
	UserGoal       string            `json:"user_goal"`
	ConversationID ID                `json:"conversation_id,omitempty"`
	WorkspacePath  string            `json:"workspace_path,omitempty"`
	WorkspaceID    ID                `json:"workspace_id,omitempty"`
	Attachments    []AttachmentInput `json:"attachments,omitempty"`
	ModelPolicy    *ModelPolicyInput `json:"model_policy,omitempty"`
}

type AttachmentInput struct {
	Name    string `json:"name"`
	Kind    string `json:"kind"`
	Content string `json:"content,omitempty"`
	Path    string `json:"path,omitempty"`
	URL     string `json:"url,omitempty"`
}

type ModelPolicyInput struct {
	Provider string `json:"provider,omitempty"`
	Model    string `json:"model,omitempty"`
	MaxSteps int    `json:"max_steps,omitempty"`
}

type CreateTaskOutput struct {
	Task         TaskDTO         `json:"task"`
	Run          AgentRunDTO     `json:"run"`
	Conversation ConversationDTO `json:"conversation"`
	Message      MessageDTO      `json:"message"`
}

type ConversationDTO struct {
	ID        ID     `json:"id"`
	Title     string `json:"title,omitempty"`
	CreatedAt string `json:"created_at"`
	UpdatedAt string `json:"updated_at,omitempty"`
}

type MessageDTO struct {
	ID             ID     `json:"id"`
	ConversationID ID     `json:"conversation_id"`
	TaskID         ID     `json:"task_id,omitempty"`
	RunID          ID     `json:"run_id,omitempty"`
	Role           string `json:"role"`
	Content        string `json:"content"`
	CreatedAt      string `json:"created_at"`
}

type ListTasksOutput struct {
	Tasks      []TaskDTO `json:"tasks"`
	NextCursor string    `json:"next_cursor,omitempty"`
}

type TaskDetailOutput struct {
	Task      TaskDTO            `json:"task"`
	ActiveRun *AgentRunDTO       `json:"active_run,omitempty"`
	Steps     []ExecutionStepDTO `json:"steps"`
	Artifacts []ArtifactDTO      `json:"artifacts"`
}

type RunIdInput struct {
	RunID ID `json:"run_id"`
}

type RetryStepInput struct {
	RunID  ID `json:"run_id"`
	StepID ID `json:"step_id"`
}

type ResolvePermissionOutput struct {
	Request PermissionRequestDTO `json:"request"`
	Events  []RuntimeEvent       `json:"events"`
}

// SettingsDTO 简化版
type SettingsDTO struct {
	Model       ModelSettingsDTO      `json:"model"`
	Workspace   WorkspaceSettingsDTO  `json:"workspace"`
	Permissions PermissionSettingsDTO `json:"permissions"`
	MCP         McpSettingsDTO        `json:"mcp"`
	Runtime     RuntimeSettingsDTO    `json:"runtime"`
}

// RuntimeSettingsDTO 是只读运行时状态，由 Gateway 从环境变量/配置读取。
// 不暴露 DSN、路径、API key、token 等敏感信息。
type RuntimeSettingsDTO struct {
	StorageBackend     string `json:"storage_backend"`
	PersistenceStatus  string `json:"persistence_status"`
	RuntimeBus         string `json:"runtime_bus"`
	ControlPlaneStatus string `json:"control_plane_status"` // "ready" | "degraded" | "unavailable"
}

type ModelSettingsDTO struct {
	CloudProvider    string `json:"cloud_provider,omitempty"`
	DefaultModel     string `json:"default_model,omitempty"`
	LocalEndpoint    string `json:"local_endpoint,omitempty"`
	FallbackEnabled  bool   `json:"fallback_enabled"`
	APIKeyConfigured bool   `json:"api_key_configured"`
}

type WorkspaceSettingsDTO struct {
	DefaultWorkspacePath  string   `json:"default_workspace_path,omitempty"`
	AllowedWorkspacePaths []string `json:"allowed_workspace_paths"`
}

type PermissionSettingsDTO struct {
	DefaultShellPolicy string `json:"default_shell_policy"`
	HighRiskPolicy     string `json:"high_risk_policy"`
}

type McpSettingsDTO struct {
	Servers []McpServerConfigDTO `json:"servers"`
}

type McpServerConfigDTO struct {
	ID        ID       `json:"id"`
	Name      string   `json:"name"`
	Transport string   `json:"transport"`
	Command   string   `json:"command,omitempty"`
	Args      []string `json:"args,omitempty"`
	URL       string   `json:"url,omitempty"`
	Enabled   bool     `json:"enabled"`
}
