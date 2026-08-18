package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/google/uuid"
	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type MemoryControlPlane interface {
	ListMemories(context.Context, string) (*controlplane.ListMemoriesResponse, error)
	CreateMemory(context.Context, controlplane.CreateMemoryRequest) (*controlplane.MemoryResponse, error)
	UpdateMemory(context.Context, string, controlplane.UpdateMemoryRequest) (*controlplane.MemoryResponse, error)
	DeleteMemory(context.Context, string) (*controlplane.MemoryResponse, error)
	ListMemoryCandidates(context.Context, string) (*controlplane.ListMemoryCandidatesResponse, error)
	UpdateMemoryCandidate(context.Context, string, controlplane.UpdateMemoryCandidateRequest) (*controlplane.MemoryCandidateResponse, error)
	ApproveMemoryCandidate(context.Context, string, controlplane.ResolveMemoryCandidateRequest) (*controlplane.ApproveMemoryCandidateResponse, error)
	RejectMemoryCandidate(context.Context, string, controlplane.ResolveMemoryCandidateRequest) (*controlplane.MemoryCandidateResponse, error)
}

var _ MemoryControlPlane = (*controlplane.Client)(nil)

type MemoryHandler struct{ controlPlane MemoryControlPlane }

func NewMemoryHandler(controlPlane MemoryControlPlane) *MemoryHandler {
	return &MemoryHandler{controlPlane: controlPlane}
}

func (h *MemoryHandler) Collection(w http.ResponseWriter, r *http.Request) {
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "记忆服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	switch r.Method {
	case http.MethodGet:
		resp, err := h.controlPlane.ListMemories(ctx, r.URL.RawQuery)
		if err != nil {
			h.writeError(w, err)
			return
		}
		items := make([]contracts.MemoryDTO, len(resp.Memories))
		for i, item := range resp.Memories {
			items[i] = memoryFromCP(item)
		}
		writeOK(w, contracts.ListMemoriesOutput{Memories: items})
	case http.MethodPost:
		var input contracts.CreateMemoryInput
		if json.NewDecoder(r.Body).Decode(&input) != nil {
			writeError(w, 400, "VALIDATION_ERROR", "请求格式无效", "validation", false)
			return
		}
		resp, err := h.controlPlane.CreateMemory(ctx, controlplane.CreateMemoryRequest{
			ScopeType: input.ScopeType, WorkspaceID: input.WorkspaceID, Category: input.Category,
			Key: input.Key, Content: input.Content, Importance: input.Importance,
		})
		if err != nil {
			h.writeError(w, err)
			return
		}
		writeOK(w, contracts.MemoryMutationOutput{Memory: memoryFromCP(resp.Memory)})
	default:
		WriteMethodNotAllowed(w, "GET, POST")
	}
}

func (h *MemoryHandler) Item(w http.ResponseWriter, r *http.Request, id string) {
	if _, err := uuid.Parse(id); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "memory_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "记忆服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	var resp *controlplane.MemoryResponse
	var err error
	switch r.Method {
	case http.MethodPatch:
		var input contracts.UpdateMemoryInput
		if json.NewDecoder(r.Body).Decode(&input) != nil {
			writeError(w, 400, "VALIDATION_ERROR", "请求格式无效", "validation", false)
			return
		}
		resp, err = h.controlPlane.UpdateMemory(ctx, id, controlplane.UpdateMemoryRequest{
			ExpectedVersion: input.ExpectedVersion, Content: input.Content, Status: input.Status, Importance: input.Importance,
		})
	case http.MethodDelete:
		resp, err = h.controlPlane.DeleteMemory(ctx, id)
	default:
		WriteMethodNotAllowed(w, "PATCH, DELETE")
		return
	}
	if err != nil {
		h.writeError(w, err)
		return
	}
	writeOK(w, contracts.MemoryMutationOutput{Memory: memoryFromCP(resp.Memory)})
}

func (h *MemoryHandler) CandidateCollection(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		WriteMethodNotAllowed(w, "GET")
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "记忆服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ListMemoryCandidates(ctx, r.URL.RawQuery)
	if err != nil {
		h.writeError(w, err)
		return
	}
	items := make([]contracts.MemoryCandidateDTO, len(resp.Candidates))
	for i, item := range resp.Candidates {
		items[i] = memoryCandidateFromCP(item)
	}
	writeOK(w, contracts.ListMemoryCandidatesOutput{Candidates: items})
}

func (h *MemoryHandler) CandidateItem(w http.ResponseWriter, r *http.Request, id, action string) {
	if _, err := uuid.Parse(id); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "candidate_id 格式无效", "validation", false)
		return
	}
	if h.controlPlane == nil {
		writeError(w, 503, "CONTROL_PLANE_UNAVAILABLE", "记忆服务不可用", "internal", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	if action == "" && r.Method == http.MethodPatch {
		var input contracts.UpdateMemoryCandidateInput
		if json.NewDecoder(r.Body).Decode(&input) != nil {
			writeError(w, 400, "VALIDATION_ERROR", "请求格式无效", "validation", false)
			return
		}
		var workspaceID *string
		if input.WorkspaceID != nil {
			value := string(*input.WorkspaceID)
			workspaceID = &value
		}
		resp, err := h.controlPlane.UpdateMemoryCandidate(ctx, id, controlplane.UpdateMemoryCandidateRequest{
			ExpectedVersion: input.ExpectedVersion, ScopeType: input.ScopeType, WorkspaceID: workspaceID,
			Category: input.Category, SuggestedKey: input.SuggestedKey, Content: input.Content, Importance: input.Importance,
		})
		if err != nil {
			h.writeError(w, err)
			return
		}
		writeOK(w, contracts.MemoryCandidateMutationOutput{Candidate: memoryCandidateFromCP(resp.Candidate)})
		return
	}
	if r.Method == http.MethodPost && (action == "approve" || action == "reject") {
		var input contracts.ResolveMemoryCandidateInput
		if json.NewDecoder(r.Body).Decode(&input) != nil {
			writeError(w, 400, "VALIDATION_ERROR", "请求格式无效", "validation", false)
			return
		}
		request := controlplane.ResolveMemoryCandidateRequest{ExpectedVersion: input.ExpectedVersion, Note: input.Note}
		if action == "approve" {
			resp, err := h.controlPlane.ApproveMemoryCandidate(ctx, id, request)
			if err != nil {
				h.writeError(w, err)
				return
			}
			writeOK(w, contracts.ApproveMemoryCandidateOutput{Candidate: memoryCandidateFromCP(resp.Candidate), Memory: memoryFromCP(resp.Memory)})
			return
		}
		resp, err := h.controlPlane.RejectMemoryCandidate(ctx, id, request)
		if err != nil {
			h.writeError(w, err)
			return
		}
		writeOK(w, contracts.MemoryCandidateMutationOutput{Candidate: memoryCandidateFromCP(resp.Candidate)})
		return
	}
	WriteMethodNotAllowed(w, "PATCH, POST")
}

func (h *MemoryHandler) writeError(w http.ResponseWriter, err error) {
	if cpErr, ok := err.(*controlplane.ControlPlaneError); ok {
		status, code, message, category, recoverable := mapControlPlaneError(cpErr)
		writeError(w, status, code, message, category, recoverable)
		return
	}
	writeError(w, 502, "CONTROL_PLANE_ERROR", "记忆操作失败", "internal", true)
}

func memoryFromCP(item controlplane.MemoryDTO) contracts.MemoryDTO {
	return contracts.MemoryDTO{
		ID: item.ID, ScopeType: item.ScopeType, WorkspaceID: item.WorkspaceID,
		Category: item.Category, Key: item.Key, Content: item.Content, Status: item.Status,
		SourceType: item.SourceType, Importance: item.Importance, Version: item.Version,
		CreatedAt: item.CreatedAt, UpdatedAt: item.UpdatedAt,
	}
}

func memoryCandidateFromCP(item controlplane.MemoryCandidateDTO) contracts.MemoryCandidateDTO {
	return contracts.MemoryCandidateDTO{
		ID: contracts.ID(item.ID), ScopeType: item.ScopeType, WorkspaceID: contracts.ID(item.WorkspaceID),
		Category: item.Category, SuggestedKey: item.SuggestedKey, Content: item.Content, Status: item.Status,
		SourceTaskID: contracts.ID(item.SourceTaskID), SourceRunID: contracts.ID(item.SourceRunID),
		Confidence: item.Confidence, Importance: item.Importance, Sensitivity: item.Sensitivity,
		ConflictMemoryID: contracts.ID(item.ConflictMemoryID), ApprovedMemoryID: contracts.ID(item.ApprovedMemoryID),
		ExtractionPolicyVersion: item.ExtractionPolicyVersion, ExpiresAt: item.ExpiresAt,
		ResolvedAt: item.ResolvedAt, ResolutionNote: item.ResolutionNote, Version: item.Version,
		CreatedAt: item.CreatedAt, UpdatedAt: item.UpdatedAt,
	}
}
