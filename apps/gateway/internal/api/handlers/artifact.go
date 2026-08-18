package handlers

import (
	"context"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type ArtifactControlPlane interface {
	GetArtifact(context.Context, string) (*controlplane.GetArtifactResponse, error)
}

var _ ArtifactControlPlane = (*controlplane.Client)(nil)

// ArtifactHandler 只代理 Control Plane 的安全投影；不读取本地文件。
type ArtifactHandler struct{ controlPlane ArtifactControlPlane }

func NewArtifactHandler(controlPlane ArtifactControlPlane) *ArtifactHandler {
	return &ArtifactHandler{controlPlane: controlPlane}
}

// GetArtifact GET /api/artifacts/{id}
func (h *ArtifactHandler) GetArtifact(w http.ResponseWriter, r *http.Request, artifactID string) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, http.MethodGet)
		return
	}
	if _, err := uuid.Parse(artifactID); err != nil {
		writeError(w, http.StatusBadRequest, "VALIDATION_ERROR", "artifact_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, http.StatusServiceUnavailable, "CONTROL_PLANE_UNAVAILABLE", "产物读取服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	response, err := h.controlPlane.GetArtifact(ctx, artifactID)
	if err != nil {
		if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
			status, code, message, category, recoverable := mapControlPlaneError(cpErr)
			writeError(w, status, code, message, category, recoverable)
			return
		}
		writeError(w, http.StatusBadGateway, "CONTROL_PLANE_ERROR", "读取产物失败", "internal", true)
		return
	}
	a := response.Artifact
	writeOK(w, contracts.ArtifactDTO{
		ID: a.ID, TaskID: a.TaskID, RunID: a.RunID, Kind: a.Kind, Title: a.Title,
		Purpose: a.Purpose,
		Producer: contracts.ArtifactProducerDTO{
			Type: a.Producer.Type, ToolCallID: a.Producer.ToolCallID,
		},
		Content: a.Content, FileSizeBytes: a.FileSizeBytes, MimeType: a.MimeType,
		ContentHash: a.ContentHash, Metadata: a.Metadata, CreatedAt: a.CreatedAt,
	})
}
