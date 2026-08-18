package handlers

import (
	"context"
	"net/http"
	"time"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type WorkspaceControlPlane interface {
	ListWorkspaces(context.Context, bool) (*controlplane.ListWorkspacesResponse, error)
	PickWorkspace(context.Context) (*controlplane.PickWorkspaceResponse, error)
	RevokeWorkspace(context.Context, string) (*controlplane.RevokeWorkspaceResponse, error)
}

var _ WorkspaceControlPlane = (*controlplane.Client)(nil)

// WorkspaceHandler 处理 Workspace 相关 API。
type WorkspaceHandler struct {
	controlPlane WorkspaceControlPlane
}

func NewWorkspaceHandler(controlPlane WorkspaceControlPlane) *WorkspaceHandler {
	return &WorkspaceHandler{controlPlane: controlPlane}
}

// ListWorkspaces GET /api/workspaces
func (h *WorkspaceHandler) ListWorkspaces(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeMethodNotAllowed(w)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "WORKSPACE_PICKER_UNAVAILABLE", "工作区服务不可用", "internal", true)
		return
	}

	includeRevoked := r.URL.Query().Get("include_revoked") == "true"
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	cpResp, err := h.controlPlane.ListWorkspaces(ctx, includeRevoked)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			st, code, msg, cat, rec := mapControlPlaneError(cpErr)
			writeError(w, st, code, msg, cat, rec)
			return
		}
		writeError(w, http.StatusServiceUnavailable, "STORAGE_ERROR", "读取工作区列表失败", "storage", false)
		return
	}

	workspaces := make([]contracts.WorkspaceDTO, len(cpResp.Workspaces))
	for i, ws := range cpResp.Workspaces {
		workspaces[i] = workspaceFromCP(ws)
	}
	writeOK(w, contracts.ListWorkspacesOutput{Workspaces: workspaces})
}

// PickWorkspace POST /api/workspaces/pick
func (h *WorkspaceHandler) PickWorkspace(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeMethodNotAllowed(w)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "WORKSPACE_PICKER_UNAVAILABLE", "该平台不支持系统目录选择器", "internal", true)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 70*time.Second) // 用户选择目录可能较慢
	defer cancel()

	cpResp, err := h.controlPlane.PickWorkspace(ctx)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			st, code, msg, cat, rec := mapControlPlaneError(cpErr)
			writeError(w, st, code, msg, cat, rec)
			return
		}
		writeError(w, http.StatusInternalServerError, "WORKSPACE_PICK_FAILED", "选择目录失败", "internal", true)
		return
	}

	output := contracts.PickWorkspaceOutput{
		Cancelled: cpResp.Cancelled,
	}
	if cpResp.Workspace != nil {
		ws := workspaceFromCP(*cpResp.Workspace)
		output.Workspace = &ws
	}
	writeOK(w, output)
}

// RevokeWorkspace DELETE /api/workspaces/{id}
func (h *WorkspaceHandler) RevokeWorkspace(w http.ResponseWriter, r *http.Request, workspaceID string) {
	if r.Method != http.MethodDelete {
		writeMethodNotAllowed(w)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "WORKSPACE_PICKER_UNAVAILABLE", "工作区服务不可用", "internal", true)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	cpResp, err := h.controlPlane.RevokeWorkspace(ctx, workspaceID)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			st, code, msg, cat, rec := mapControlPlaneError(cpErr)
			writeError(w, st, code, msg, cat, rec)
			return
		}
		writeError(w, http.StatusInternalServerError, "WORKSPACE_PICK_FAILED", "撤销工作区失败", "internal", true)
		return
	}

	ws := workspaceFromCP(cpResp.Workspace)
	writeOK(w, contracts.RevokeWorkspaceOutput{Workspace: ws})
}

func workspaceFromCP(cp controlplane.WorkspaceDTO) contracts.WorkspaceDTO {
	return contracts.WorkspaceDTO{
		ID:            contracts.ID(cp.ID),
		Name:          cp.Name,
		RootPath:      cp.RootPath,
		CanonicalPath: cp.CanonicalPath,
		Status:        cp.Status,
		Source:        cp.Source,
		CreatedAt:     cp.CreatedAt,
		UpdatedAt:     cp.UpdatedAt,
		RevokedAt:     cp.RevokedAt,
	}
}
