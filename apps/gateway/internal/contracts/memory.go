package contracts

// -- Long-term Memory --

type MemoryDTO struct {
	ID          ID     `json:"id"`
	ScopeType   string `json:"scope_type"`
	WorkspaceID ID     `json:"workspace_id,omitempty"`
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

type MemoryCandidateDTO struct {
	ID                      ID      `json:"id"`
	ScopeType               string  `json:"scope_type"`
	WorkspaceID             ID      `json:"workspace_id,omitempty"`
	Category                string  `json:"category"`
	SuggestedKey            string  `json:"suggested_key"`
	Content                 string  `json:"content"`
	Status                  string  `json:"status"`
	SourceTaskID            ID      `json:"source_task_id"`
	SourceRunID             ID      `json:"source_run_id"`
	Confidence              float64 `json:"confidence"`
	Importance              int     `json:"importance"`
	Sensitivity             string  `json:"sensitivity"`
	ConflictMemoryID        ID      `json:"conflict_memory_id,omitempty"`
	ApprovedMemoryID        ID      `json:"approved_memory_id,omitempty"`
	ExtractionPolicyVersion string  `json:"extraction_policy_version"`
	ExpiresAt               string  `json:"expires_at,omitempty"`
	ResolvedAt              string  `json:"resolved_at,omitempty"`
	ResolutionNote          string  `json:"resolution_note,omitempty"`
	Version                 int     `json:"version"`
	CreatedAt               string  `json:"created_at"`
	UpdatedAt               string  `json:"updated_at"`
}

type CreateMemoryInput struct {
	ScopeType   string `json:"scope_type"`
	WorkspaceID ID     `json:"workspace_id,omitempty"`
	Category    string `json:"category"`
	Key         string `json:"key"`
	Content     string `json:"content"`
	Importance  int    `json:"importance"`
}

type UpdateMemoryInput struct {
	ExpectedVersion int     `json:"expected_version"`
	Content         *string `json:"content,omitempty"`
	Status          *string `json:"status,omitempty"`
	Importance      *int    `json:"importance,omitempty"`
}

type ListMemoriesOutput struct {
	Memories []MemoryDTO `json:"memories"`
}
type MemoryMutationOutput struct {
	Memory MemoryDTO `json:"memory"`
}

type UpdateMemoryCandidateInput struct {
	ExpectedVersion int     `json:"expected_version"`
	ScopeType       *string `json:"scope_type,omitempty"`
	WorkspaceID     *ID     `json:"workspace_id,omitempty"`
	Category        *string `json:"category,omitempty"`
	SuggestedKey    *string `json:"suggested_key,omitempty"`
	Content         *string `json:"content,omitempty"`
	Importance      *int    `json:"importance,omitempty"`
}

type ResolveMemoryCandidateInput struct {
	ExpectedVersion int    `json:"expected_version"`
	Note            string `json:"note,omitempty"`
}

type ListMemoryCandidatesOutput struct {
	Candidates []MemoryCandidateDTO `json:"candidates"`
}

type MemoryCandidateMutationOutput struct {
	Candidate MemoryCandidateDTO `json:"candidate"`
}

type ApproveMemoryCandidateOutput struct {
	Candidate MemoryCandidateDTO `json:"candidate"`
	Memory    MemoryDTO          `json:"memory"`
}
