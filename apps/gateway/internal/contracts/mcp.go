package contracts

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
type ListMcpServersOutput struct {
	Servers               []McpServerDTO `json:"servers"`
	WorkerRestartRequired bool           `json:"worker_restart_required,omitempty"`
}
type CreateMcpServerInput struct {
	Slug    string   `json:"slug"`
	Name    string   `json:"name"`
	Command string   `json:"command"`
	Args    []string `json:"args"`
	EnvKeys []string `json:"env_keys"`
}
type UpdateMcpServerInput struct {
	Enabled         bool `json:"enabled"`
	ExpectedVersion int  `json:"expected_version"`
}
type McpServerMutationOutput struct {
	Server                McpServerDTO `json:"server"`
	WorkerRestartRequired bool         `json:"worker_restart_required"`
}

type McpDiscoveryRefreshOutput struct {
	CommandID             string `json:"command_id"`
	Status                string `json:"status"`
	WorkerRestartRequired bool   `json:"worker_restart_required"`
}
