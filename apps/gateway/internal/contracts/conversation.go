package contracts

// -- Conversation --

// ConversationMessage 保存一条对话消息（MVP 最小化）。
// 对齐 docs/14-data-schema.md § conversations。
type ConversationMessage struct {
	// ID 是消息唯一标识。
	ID ID `json:"id"`
	// TaskID 是关联的 Task id（可选，独立对话可为空）。
	TaskID ID `json:"task_id,omitempty"`
	// RunID 是关联的 Run id（可选）。
	RunID ID `json:"run_id,omitempty"`
	// Role 是发送者角色：user / assistant / system。
	Role string `json:"role"`
	// Content 是消息文本内容。
	Content string `json:"content"`
	// CreatedAt 是消息创建时间（ISO 8601）。
	CreatedAt string `json:"created_at"`
}

// -- Conversation API（多轮对话 MVP）--

type ListConversationsOutput struct {
	Conversations []ConversationDTO `json:"conversations"`
}

type ConversationDetailOutput struct {
	Conversation ConversationDTO `json:"conversation"`
	Messages     []MessageDTO    `json:"messages"`
	NextCursor   *string         `json:"next_cursor,omitempty"`
}

// 时间工具
