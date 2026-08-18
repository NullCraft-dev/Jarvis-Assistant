package contracts

// -- Workspace --

type WorkspaceDTO struct {
	ID            ID      `json:"id"`
	Name          string  `json:"name"`
	RootPath      string  `json:"root_path"`
	CanonicalPath string  `json:"canonical_path"`
	Status        string  `json:"status"`
	Source        string  `json:"source"`
	CreatedAt     string  `json:"created_at"`
	UpdatedAt     string  `json:"updated_at"`
	RevokedAt     *string `json:"revoked_at,omitempty"`
}

type ListWorkspacesOutput struct {
	Workspaces []WorkspaceDTO `json:"workspaces"`
}

type PickWorkspaceOutput struct {
	Workspace *WorkspaceDTO `json:"workspace"`
	Cancelled bool          `json:"cancelled"`
}

type RevokeWorkspaceOutput struct {
	Workspace WorkspaceDTO `json:"workspace"`
}
