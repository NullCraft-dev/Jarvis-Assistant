package handlers

import (
	"context"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type SettingsControlPlane interface {
	HealthCheck(context.Context) (*controlplane.HealthResponse, error)
	GetModelConfig(context.Context) (*controlplane.ModelConfigResponse, error)
}

var _ SettingsControlPlane = (*controlplane.Client)(nil)

// SettingsHandler 处理设置相关 API。
type SettingsHandler struct {
	controlPlane SettingsControlPlane
}

func NewSettingsHandler(controlPlane SettingsControlPlane) *SettingsHandler {
	return &SettingsHandler{controlPlane: controlPlane}
}

// GetSettings GET /api/settings
func (h *SettingsHandler) GetSettings(w http.ResponseWriter, r *http.Request) {
	workspaceRoot := canonicalWorkspacePath(os.Getenv("JARVIS_WORKSPACE_ROOT"))
	allowedWorkspacePaths := configuredWorkspacePaths(
		workspaceRoot,
		os.Getenv("JARVIS_ALLOWED_WORKSPACE_PATHS"),
	)

	persistenceStatus := "unavailable"
	controlPlaneStatus := "unavailable"
	modelProvider := ""
	modelName := ""
	modelAPIKeyConfigured := false
	if h.controlPlane != nil {
		healthCtx, cancelHealth := context.WithTimeout(r.Context(), 2*time.Second)
		if health, err := h.controlPlane.HealthCheck(healthCtx); err == nil {
			if health.Status == "ok" {
				controlPlaneStatus = "ready"
			} else {
				controlPlaneStatus = "degraded"
			}
			if health.Database == "connected" {
				persistenceStatus = "ready"
			} else {
				persistenceStatus = "degraded"
			}
		}
		cancelHealth()

		modelCtx, cancelModel := context.WithTimeout(r.Context(), 2*time.Second)
		if modelConfig, err := h.controlPlane.GetModelConfig(modelCtx); err == nil {
			modelProvider = modelConfig.Provider
			modelName = modelConfig.ModelName
			modelAPIKeyConfigured = modelConfig.APIKeyConfigured
		}
		cancelModel()
	}
	runtimeBus := os.Getenv("JARVIS_RUNTIME_BUS")
	if runtimeBus == "" {
		runtimeBus = "redis"
	}

	settings := contracts.SettingsDTO{
		Model: contracts.ModelSettingsDTO{
			CloudProvider:    modelProvider,
			DefaultModel:     modelName,
			FallbackEnabled:  false,
			APIKeyConfigured: modelAPIKeyConfigured,
		},
		Workspace: contracts.WorkspaceSettingsDTO{
			DefaultWorkspacePath:  workspaceRoot,
			AllowedWorkspacePaths: allowedWorkspacePaths,
		},
		Permissions: contracts.PermissionSettingsDTO{
			DefaultShellPolicy: "confirm",
			HighRiskPolicy:     "always_confirm",
		},
		MCP: contracts.McpSettingsDTO{
			Servers: []contracts.McpServerConfigDTO{},
		},
		Runtime: contracts.RuntimeSettingsDTO{
			StorageBackend: getStorageBackend(), PersistenceStatus: persistenceStatus,
			RuntimeBus: runtimeBus, ControlPlaneStatus: controlPlaneStatus,
		},
	}

	writeOK(w, settings)
}

func configuredWorkspacePaths(defaultPath, rawAllowed string) []string {
	configured := []string{}
	if defaultPath != "" {
		configured = append(configured, defaultPath)
	}
	if rawAllowed != "" {
		configured = append(configured, filepath.SplitList(rawAllowed)...)
	}

	result := []string{}
	seen := map[string]bool{}
	for _, raw := range configured {
		path := canonicalWorkspacePath(raw)
		if path == "" || seen[path] {
			continue
		}
		seen[path] = true
		result = append(result, path)
	}
	return result
}

func canonicalWorkspacePath(raw string) string {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return ""
	}
	abs, err := filepath.Abs(trimmed)
	if err != nil {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		return filepath.Clean(resolved)
	}
	return filepath.Clean(abs)
}

func getStorageBackend() string {
	// PostgreSQL 是唯一持久化真相（2026-07-14 架构重构）
	return "postgresql"
}
