package app

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"github.com/jarvis-assistant/gateway/internal/api/handlers"
	"github.com/jarvis-assistant/gateway/internal/api/middleware"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
	"github.com/jarvis-assistant/gateway/internal/orchestrator"
)

func buildRouter(
	cfg orchestrator.RuntimeBusConfig,
	runtimeBus orchestrator.RuntimeBus,
	stateStore orchestrator.RuntimeStateStore,
	cpClient *controlplane.Client,
) http.Handler {
	var workerStatusProvider handlers.WorkerStatusProvider
	if wsp, ok := runtimeBus.(handlers.WorkerStatusProvider); ok {
		workerStatusProvider = wsp
	}
	var runtimeHealthProvider handlers.RuntimeHealthProvider
	if rhp, ok := runtimeBus.(handlers.RuntimeHealthProvider); ok {
		runtimeHealthProvider = rhp
	}

	var workerStatusFn handlers.WorkerStatusFn
	if workerStatusProvider != nil {
		workerStatusFn = func() (string, *string, *string) {
			workers := workerStatusProvider.GetWorkerStatuses()
			if len(workers) == 0 {
				return "unknown", nil, nil
			}
			w := workers[0]
			var lastHB *string
			if w.LastSeenAt != "" {
				lastHB = &w.LastSeenAt
			}
			var lastErr *string
			if w.Model != nil && w.Model.LastErrorCode != nil && *w.Model.LastErrorCode != "" {
				lastErr = w.Model.LastErrorCode
			}
			return w.Status, lastHB, lastErr
		}
	}

	taskHandler := handlers.NewTaskHandler(runtimeBus, stateStore, cpClient)
	runHandler := handlers.NewRunHandler(runtimeBus, stateStore, cpClient)
	settingsHandler := handlers.NewSettingsHandler(cpClient)
	modelConfigHandler := handlers.NewModelConfigHandler(cpClient, workerStatusFn)
	auditLogHandler := handlers.NewAuditLogHandler(cpClient)
	artifactHandler := handlers.NewArtifactHandler(cpClient)
	memoryHandler := handlers.NewMemoryHandler(cpClient)
	workspaceHandler := handlers.NewWorkspaceHandler(cpClient)
	knowledgeHandler := handlers.NewKnowledgeHandler(cpClient)
	ragHandler := handlers.NewRagHandler(cpClient)
	scheduleHandler := handlers.NewScheduleHandler(cpClient)
	var mcpDiscoveryPublisher handlers.McpDiscoveryPublisher
	if publisher, ok := runtimeBus.(handlers.McpDiscoveryPublisher); ok {
		mcpDiscoveryPublisher = publisher
	}
	mcpHandler := handlers.NewMcpHandler(cpClient, mcpDiscoveryPublisher)
	workerHandler := handlers.NewWorkerHandler(workerStatusProvider)
	runtimeHealthHandler := handlers.NewRuntimeHealthHandler(runtimeHealthProvider, cpClient)

	mux := http.NewServeMux()

	mux.HandleFunc("/api/tasks", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			taskHandler.CreateTask(w, r)
		case http.MethodGet:
			taskHandler.ListTasks(w, r)
		default:
			handlers.WriteMethodNotAllowed(w, "GET, POST")
		}
	})

	mux.HandleFunc("/api/tasks/", func(w http.ResponseWriter, r *http.Request) {
		taskID := handlers.ExtractIDFromPath(r.URL.Path, "/api/tasks/")
		if taskID == "" {
			handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "缺少任务 ID", "validation", false)
			return
		}
		taskHandler.GetTask(w, r, taskID)
	})

	mux.HandleFunc("/api/artifacts/", func(w http.ResponseWriter, r *http.Request) {
		artifactID := handlers.ExtractIDFromPath(r.URL.Path, "/api/artifacts/")
		if artifactID == "" {
			handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "缺少产物 ID", "validation", false)
			return
		}
		artifactHandler.GetArtifact(w, r, artifactID)
	})

	mux.HandleFunc("/api/memories", memoryHandler.Collection)
	mux.HandleFunc("/api/memories/", func(w http.ResponseWriter, r *http.Request) {
		memoryID := handlers.ExtractIDFromPath(r.URL.Path, "/api/memories/")
		if memoryID == "" {
			handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "缺少记忆 ID", "validation", false)
			return
		}
		memoryHandler.Item(w, r, memoryID)
	})
	mux.HandleFunc("/api/memory-candidates", memoryHandler.CandidateCollection)
	mux.HandleFunc("/api/memory-candidates/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/api/memory-candidates/")
		parts := strings.Split(strings.Trim(path, "/"), "/")
		if len(parts) < 1 || parts[0] == "" {
			handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "缺少记忆候选 ID", "validation", false)
			return
		}
		action := ""
		if len(parts) == 2 {
			action = parts[1]
		} else if len(parts) > 2 {
			handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "记忆候选路径无效", "validation", false)
			return
		}
		memoryHandler.CandidateItem(w, r, parts[0], action)
	})
	mux.HandleFunc("/api/knowledge-vaults", knowledgeHandler.Collection)
	mux.HandleFunc("/api/rag/documents", ragHandler.Documents)
	mux.HandleFunc("/api/rag/upload-requests", ragHandler.UploadRequests)
	mux.HandleFunc("/api/rag/feedback", ragHandler.Feedback)
	mux.HandleFunc("/api/rag/evaluation/traces", ragHandler.EvaluationTraces)
	mux.HandleFunc("/api/rag/evaluation/gates", ragHandler.EvaluationGates)
	mux.HandleFunc("/api/rag/evaluation/issues", ragHandler.EvaluationQualityIssues)
	mux.HandleFunc("/api/rag/evaluation/gates/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/rag/evaluation/gates/"), "/"), "/")
		if len(parts) != 2 || parts[0] == "" || parts[1] != "failure-targets" {
			handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
			return
		}
		ragHandler.EvaluationGateFailureTargets(w, r, parts[0])
	})
	mux.HandleFunc("/api/rag/evaluation/issues/", func(w http.ResponseWriter, r *http.Request) {
		issueID := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/rag/evaluation/issues/"), "/")
		if issueID == "" || strings.Contains(issueID, "/") {
			handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
			return
		}
		ragHandler.EvaluationQualityIssue(w, r, issueID)
	})
	mux.HandleFunc("/api/rag/evaluation/traces/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/rag/evaluation/traces/"), "/"), "/")
		if len(parts) == 0 || parts[0] == "" || len(parts) > 2 || (len(parts) == 2 && parts[1] != "privacy" && parts[1] != "label" && parts[1] != "promote") {
			handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
			return
		}
		action := ""
		if len(parts) == 2 {
			action = parts[1]
		}
		ragHandler.EvaluationTraceItem(w, r, parts[0], action)
	})
	mux.HandleFunc("/api/rag/feedback/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/rag/feedback/"), "/"), "/")
		if len(parts) == 0 || parts[0] == "" || len(parts) > 2 || (len(parts) == 2 && parts[1] != "triage") {
			handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
			return
		}
		action := ""
		if len(parts) == 2 {
			action = parts[1]
		}
		ragHandler.FeedbackItem(w, r, parts[0], action)
	})
	mux.HandleFunc("/api/rag/documents/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/api/rag/documents/")
		parts := strings.Split(strings.Trim(path, "/"), "/")
		if len(parts) == 1 && parts[0] != "" {
			ragHandler.Item(w, r, parts[0], "")
			return
		}
		if len(parts) == 2 && parts[0] != "" && (parts[1] == "restart" || parts[1] == "cancel" || parts[1] == "delete-requests") {
			ragHandler.Item(w, r, parts[0], parts[1])
			return
		}
		handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
	})
	mux.HandleFunc("/api/rag/delete-requests/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/api/rag/delete-requests/")
		parts := strings.Split(strings.Trim(path, "/"), "/")
		if len(parts) == 2 && parts[0] != "" && parts[1] == "resolve" {
			ragHandler.ResolveDelete(w, r, parts[0])
			return
		}
		handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
	})
	mux.HandleFunc("/api/rag/upload-requests/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/api/rag/upload-requests/")
		parts := strings.Split(strings.Trim(path, "/"), "/")
		if len(parts) == 2 && parts[0] != "" && parts[1] == "resolve" {
			ragHandler.ResolveUploadRequest(w, r, parts[0])
			return
		}
		handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
	})
	mux.HandleFunc("/api/knowledge-vaults/connect", knowledgeHandler.Connect)
	mux.HandleFunc("/api/knowledge-vaults/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/api/knowledge-vaults/")
		parts := strings.Split(strings.Trim(path, "/"), "/")
		if len(parts) == 2 && parts[0] != "" && parts[1] == "documents" {
			knowledgeHandler.Documents(w, r, parts[0])
			return
		}
		handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
	})
	mux.HandleFunc("/api/scheduled-tasks", scheduleHandler.Collection)
	mux.HandleFunc("/api/scheduled-tasks/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/api/scheduled-tasks/")
		parts := strings.Split(strings.Trim(path, "/"), "/")
		if len(parts) < 1 || parts[0] == "" {
			handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "缺少计划任务 ID", "validation", false)
			return
		}
		action := ""
		if len(parts) == 2 {
			action = parts[1]
		} else if len(parts) > 2 {
			handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "计划任务路径无效", "validation", false)
			return
		}
		scheduleHandler.Item(w, r, parts[0], action)
	})
	mux.HandleFunc("/api/mcp-servers", mcpHandler.Collection)
	mux.HandleFunc("/api/mcp-servers/refresh", mcpHandler.Refresh)
	mux.HandleFunc("/api/mcp-servers/builtin/literature", mcpHandler.ConnectBuiltinLiterature)
	mux.HandleFunc("/api/mcp-servers/", func(w http.ResponseWriter, r *http.Request) {
		serverID := handlers.ExtractIDFromPath(r.URL.Path, "/api/mcp-servers/")
		if serverID == "" {
			handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "缺少 MCP 服务 ID", "validation", false)
			return
		}
		mcpHandler.Item(w, r, serverID)
	})

	mux.HandleFunc("/api/runs/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if strings.HasSuffix(path, "/retry") && strings.Contains(path, "/steps/") {
			parts := strings.Split(strings.TrimPrefix(path, "/api/runs/"), "/")
			if len(parts) == 4 && parts[1] == "steps" && parts[3] == "retry" {
				runHandler.RetryFailedStep(w, r, parts[0], parts[2])
				return
			}
		}
		if strings.HasSuffix(path, "/permissions") {
			runID := handlers.ExtractIDFromPath(strings.TrimSuffix(path, "/permissions"), "/api/runs/")
			runHandler.ListPendingPermissions(w, r, runID)
			return
		}
		if strings.HasSuffix(path, "/events") {
			runID := handlers.ExtractIDFromPath(strings.TrimSuffix(path, "/events"), "/api/runs/")
			if runID == "" {
				handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "缺少运行 ID", "validation", false)
				return
			}
			runHandler.SubscribeEvents(w, r, runID)
			return
		}
		if strings.HasSuffix(path, "/pause") {
			runID := handlers.ExtractIDFromPath(strings.TrimSuffix(path, "/pause"), "/api/runs/")
			runHandler.PauseRun(w, r, runID)
			return
		}
		if strings.HasSuffix(path, "/resume") {
			runID := handlers.ExtractIDFromPath(strings.TrimSuffix(path, "/resume"), "/api/runs/")
			runHandler.ResumeRun(w, r, runID)
			return
		}
		if strings.HasSuffix(path, "/cancel") {
			runID := handlers.ExtractIDFromPath(strings.TrimSuffix(path, "/cancel"), "/api/runs/")
			runHandler.CancelRun(w, r, runID)
			return
		}
		handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
	})

	mux.HandleFunc("/api/permissions/resolve", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			handlers.WriteMethodNotAllowed(w, http.MethodPost)
			return
		}
		runHandler.ResolvePermission(w, r)
	})

	mux.HandleFunc("/api/settings", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			settingsHandler.GetSettings(w, r)
		default:
			handlers.WriteMethodNotAllowed(w, http.MethodGet)
		}
	})

	// Phase 6: Model Config API
	// GET /api/model-config — 模型配置投影
	mux.HandleFunc("/api/model-config", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			handlers.WriteMethodNotAllowed(w, http.MethodGet)
			return
		}
		modelConfigHandler.GetModelConfig(w, r)
	})

	// POST /api/model-config/test — 模型连通性测试
	mux.HandleFunc("/api/model-config/test", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			handlers.WriteMethodNotAllowed(w, http.MethodPost)
			return
		}
		modelConfigHandler.TestModelConnection(w, r)
	})

	// GET /api/audit-logs — PostgreSQL AuditLog 的安全只读查询投影
	mux.HandleFunc("/api/audit-logs", auditLogHandler.ListAuditLogs)
	// GET /api/audit-logs/export — 有界、分页生成的安全审计导出流
	mux.HandleFunc("/api/audit-logs/export", auditLogHandler.ExportAuditLogs)
	// GET /api/audit-logs/retention/preview — 只读保留策略预演
	mux.HandleFunc("/api/audit-logs/retention/preview", auditLogHandler.PreviewAuditRetention)
	// POST /api/audit-logs/retention/requests — 创建 L4 单次清理确认
	mux.HandleFunc("/api/audit-logs/retention/requests", auditLogHandler.CreateAuditRetentionRequest)
	// POST /api/audit-logs/retention/requests/{id}/resolve — 批准或拒绝有界清理
	mux.HandleFunc("/api/audit-logs/retention/requests/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/api/audit-logs/retention/requests/")
		requestID := strings.TrimSuffix(path, "/resolve")
		if requestID == path || strings.Contains(requestID, "/") || requestID == "" {
			handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
			return
		}
		auditLogHandler.ResolveAuditRetentionRequest(w, r, requestID)
	})

	mux.HandleFunc("/api/runtime/workers", workerHandler.GetWorkers)
	mux.HandleFunc("/api/runtime/health", runtimeHealthHandler.GetRuntimeHealth)
	mux.HandleFunc("/api/runtime/storage-reconciliation", runtimeHealthHandler.GetStorageReconciliation)
	mux.HandleFunc("/api/runtime/storage-reconciliation/repairs/inspect", runtimeHealthHandler.InspectTerminalEventRepair)
	mux.HandleFunc("/api/runtime/storage-reconciliation/repairs/requests", runtimeHealthHandler.CreateTerminalEventRepairRequest)
	mux.HandleFunc("/api/runtime/storage-reconciliation/repairs/requests/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/api/runtime/storage-reconciliation/repairs/requests/")
		if !strings.HasSuffix(path, "/resolve") {
			handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
			return
		}
		requestID := strings.TrimSuffix(path, "/resolve")
		runtimeHealthHandler.ResolveTerminalEventRepairRequest(w, r, requestID)
	})
	mux.HandleFunc("/api/runtime/dead-letters", runtimeHealthHandler.ListDeadLetters)
	mux.HandleFunc("/api/runtime/dead-letters/retry/inspect", runtimeHealthHandler.InspectDeadLetterRetry)
	mux.HandleFunc("/api/runtime/dead-letters/retry/requests", runtimeHealthHandler.CreateDeadLetterRetryRequest)
	mux.HandleFunc("/api/runtime/dead-letters/retry/requests/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimPrefix(r.URL.Path, "/api/runtime/dead-letters/retry/requests/")
		if !strings.HasSuffix(path, "/resolve") {
			handlers.WriteAppError(w, http.StatusNotFound, "NOT_FOUND", "资源不存在", "not_found", false)
			return
		}
		requestID := strings.TrimSuffix(path, "/resolve")
		runtimeHealthHandler.ResolveDeadLetterRetryRequest(w, r, requestID)
	})

	// 多轮对话 MVP — 会话 API
	mux.HandleFunc("/api/conversations", func(w http.ResponseWriter, r *http.Request) {
		taskHandler.ListConversations(w, r)
	})

	mux.HandleFunc("/api/conversations/", func(w http.ResponseWriter, r *http.Request) {
		convID := handlers.ExtractIDFromPath(r.URL.Path, "/api/conversations/")
		taskHandler.GetConversation(w, r, convID)
	})

	// Workspace API
	mux.HandleFunc("/api/workspaces", func(w http.ResponseWriter, r *http.Request) {
		workspaceHandler.ListWorkspaces(w, r)
	})

	mux.HandleFunc("/api/workspaces/pick", func(w http.ResponseWriter, r *http.Request) {
		workspaceHandler.PickWorkspace(w, r)
	})

	mux.HandleFunc("/api/workspaces/", func(w http.ResponseWriter, r *http.Request) {
		wsID := handlers.ExtractIDFromPath(r.URL.Path, "/api/workspaces/")
		if wsID == "" {
			handlers.WriteAppError(w, http.StatusBadRequest, "VALIDATION_ERROR", "缺少工作区 ID", "validation", false)
			return
		}
		workspaceHandler.RevokeWorkspace(w, r, wsID)
	})

	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		dbStatus := "unavailable"
		if cpClient != nil {
			ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
			defer cancel()
			if hr, err := cpClient.HealthCheck(ctx); err == nil && hr.Status == "ok" {
				dbStatus = "ready"
			} else if err == nil {
				dbStatus = "degraded"
			}
		}
		status := "degraded"
		if dbStatus == "ready" {
			status = "healthy"
		}
		json.NewEncoder(w).Encode(map[string]interface{}{
			"ok": true,
			"data": map[string]interface{}{
				"status":               status,
				"persistence_backend":  "postgresql",
				"persistence_status":   dbStatus,
				"runtime_bus":          cfg.BusType,
				"control_plane_status": dbStatus,
			},
		})
	})

	return middleware.Logging(middleware.CORS(mux))
}
