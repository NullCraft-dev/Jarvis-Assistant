package contracts

// -- 基础类型 --

type ID = string

// -- 统一返回与错误 --

type ApiResult[T any] struct {
	Ok    bool      `json:"ok"`
	Data  *T        `json:"data,omitempty"`
	Error *AppError `json:"error,omitempty"`
}

type AppError struct {
	Code        string                 `json:"code"`
	Message     string                 `json:"message"`
	Category    string                 `json:"category"`
	Recoverable bool                   `json:"recoverable"`
	Details     map[string]interface{} `json:"details,omitempty"`
	CauseID     string                 `json:"cause_id,omitempty"`
}

// -- 风险等级 --

type RiskLevel = string
