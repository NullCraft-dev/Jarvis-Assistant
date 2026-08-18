package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type KnowledgeControlPlane interface {
	ListKnowledgeVaults(context.Context) (*controlplane.ListKnowledgeVaultsResponse, error)
	ConnectKnowledgeVault(context.Context, controlplane.ConnectKnowledgeVaultRequest) (*controlplane.KnowledgeVaultResponse, error)
	ListKnowledgeDocuments(context.Context, string) (*controlplane.ListKnowledgeDocumentsResponse, error)
	CreateKnowledgeDocument(context.Context, string, controlplane.CreateKnowledgeDocumentRequest) (*controlplane.KnowledgeDocumentResponse, error)
}

type KnowledgeHandler struct{ controlPlane KnowledgeControlPlane }

func NewKnowledgeHandler(cp KnowledgeControlPlane) *KnowledgeHandler {
	return &KnowledgeHandler{controlPlane: cp}
}

func (h *KnowledgeHandler) Collection(w http.ResponseWriter, r *http.Request) {
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "知识库服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	switch r.Method {
	case http.MethodGet:
		resp, err := h.controlPlane.ListKnowledgeVaults(ctx)
		if err != nil {
			h.writeError(w, err)
			return
		}
		vaults := make([]contracts.KnowledgeVaultDTO, len(resp.Vaults))
		for i, item := range resp.Vaults {
			vaults[i] = knowledgeVaultFromCP(item)
		}
		writeOK(w, contracts.ListKnowledgeVaultsOutput{Vaults: vaults, SuggestedPath: resp.SuggestedPath})
	default:
		WriteMethodNotAllowed(w, "GET")
	}
}

func (h *KnowledgeHandler) Connect(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, "POST")
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "知识库服务不可用", "internal", true)
		return
	}
	var input contracts.ConnectKnowledgeVaultInput
	if json.NewDecoder(r.Body).Decode(&input) != nil || strings.TrimSpace(input.Path) == "" {
		writeError(w, 400, "VALIDATION_ERROR", "知识库路径不能为空", "validation", false)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ConnectKnowledgeVault(ctx, controlplane.ConnectKnowledgeVaultRequest{Path: input.Path})
	if err != nil {
		h.writeError(w, err)
		return
	}
	writeOK(w, contracts.KnowledgeVaultMutationOutput{Vault: knowledgeVaultFromCP(resp.Vault)})
}

func (h *KnowledgeHandler) Documents(w http.ResponseWriter, r *http.Request, vaultID string) {
	if _, err := uuid.Parse(vaultID); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "vault_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "知识库服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	switch r.Method {
	case http.MethodGet:
		resp, err := h.controlPlane.ListKnowledgeDocuments(ctx, vaultID)
		if err != nil {
			h.writeError(w, err)
			return
		}
		items := make([]contracts.KnowledgeDocumentDTO, len(resp.Documents))
		for i, item := range resp.Documents {
			items[i] = knowledgeDocumentFromCP(item)
		}
		writeOK(w, contracts.ListKnowledgeDocumentsOutput{Documents: items})
	case http.MethodPost:
		var input contracts.CreateKnowledgeDocumentInput
		if json.NewDecoder(r.Body).Decode(&input) != nil || strings.TrimSpace(input.Title) == "" || strings.TrimSpace(input.Content) == "" {
			writeError(w, 400, "VALIDATION_ERROR", "标题和正文不能为空", "validation", false)
			return
		}
		resp, err := h.controlPlane.CreateKnowledgeDocument(ctx, vaultID, controlplane.CreateKnowledgeDocumentRequest{Title: input.Title, Kind: input.Kind, Content: input.Content, Tags: input.Tags, SourceURLs: input.SourceURLs})
		if err != nil {
			h.writeError(w, err)
			return
		}
		writeOK(w, contracts.KnowledgeDocumentMutationOutput{Document: knowledgeDocumentFromCP(resp.Document)})
	default:
		WriteMethodNotAllowed(w, "GET, POST")
	}
}

func (h *KnowledgeHandler) writeError(w http.ResponseWriter, err error) {
	if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
		st, code, msg, cat, rec := mapControlPlaneError(cpErr)
		writeError(w, st, code, msg, cat, rec)
		return
	}
	writeError(w, 503, "KNOWLEDGE_SERVICE_ERROR", "知识库服务暂不可用", "storage", true)
}

func knowledgeVaultFromCP(v controlplane.KnowledgeVaultDTO) contracts.KnowledgeVaultDTO {
	return contracts.KnowledgeVaultDTO{ID: v.ID, Name: v.Name, RootPath: v.RootPath, CanonicalPath: v.CanonicalPath, Status: v.Status, Source: v.Source, CreatedAt: v.CreatedAt, UpdatedAt: v.UpdatedAt}
}
func knowledgeDocumentFromCP(d controlplane.KnowledgeDocumentDTO) contracts.KnowledgeDocumentDTO {
	return contracts.KnowledgeDocumentDTO{ID: d.ID, VaultID: d.VaultID, Title: d.Title, Kind: d.Kind, RelativePath: d.RelativePath, ContentHash: d.ContentHash, SizeBytes: d.SizeBytes, Tags: d.Tags, SourceURLs: d.SourceURLs, SourceTaskID: d.SourceTaskID, SourceRunID: d.SourceRunID, CreatedAt: d.CreatedAt, UpdatedAt: d.UpdatedAt}
}
