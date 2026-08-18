package contracts

// -- 内存状态 --

// InMemoryState 保存 mock 运行时状态
type InMemoryState struct {
	Tasks          map[ID]*TaskDTO
	Runs           map[ID]*AgentRunDTO
	Steps          map[ID][]ExecutionStepDTO
	PermissionReqs map[ID]*PermissionRequestDTO
	Events         map[ID][]RuntimeEvent
}

func NewInMemoryState() *InMemoryState {
	return &InMemoryState{
		Tasks:          make(map[ID]*TaskDTO),
		Runs:           make(map[ID]*AgentRunDTO),
		Steps:          make(map[ID][]ExecutionStepDTO),
		PermissionReqs: make(map[ID]*PermissionRequestDTO),
		Events:         make(map[ID][]RuntimeEvent),
	}
}
