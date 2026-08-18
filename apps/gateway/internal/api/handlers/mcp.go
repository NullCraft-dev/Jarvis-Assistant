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

type McpControlPlane interface {
	ListMcpServers(context.Context) (*controlplane.ListMcpServersResponse, error)
	CreateMcpServer(context.Context, controlplane.CreateMcpServerRequest) (*controlplane.McpServerResponse, error)
	ConnectBuiltinLiteratureServer(context.Context) (*controlplane.McpServerResponse, error)
	UpdateMcpServer(context.Context, string, controlplane.UpdateMcpServerRequest) (*controlplane.McpServerResponse, error)
}

func (h *McpHandler) ConnectBuiltinLiterature(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, "POST")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.ConnectBuiltinLiteratureServer(ctx)
	if err != nil {
		h.writeError(w, err)
		return
	}
	writeOK(w, contracts.McpServerMutationOutput{
		Server: mcpServer(resp.Server), WorkerRestartRequired: resp.WorkerRestartRequired,
	})
}

type McpDiscoveryPublisher interface {
	RequestMcpDiscovery(context.Context) (string, error)
}
type McpHandler struct {
	controlPlane McpControlPlane
	publisher    McpDiscoveryPublisher
}

func NewMcpHandler(cp McpControlPlane, publisher McpDiscoveryPublisher) *McpHandler {
	return &McpHandler{controlPlane: cp, publisher: publisher}
}
func (h *McpHandler) Collection(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	switch r.Method {
	case http.MethodGet:
		resp, err := h.controlPlane.ListMcpServers(ctx)
		if err != nil {
			h.writeError(w, err)
			return
		}
		writeOK(w, mcpList(resp))
	case http.MethodPost:
		var input contracts.CreateMcpServerInput
		if json.NewDecoder(r.Body).Decode(&input) != nil {
			writeError(w, 400, "VALIDATION_ERROR", "请求格式无效", "validation", false)
			return
		}
		resp, err := h.controlPlane.CreateMcpServer(ctx, controlplane.CreateMcpServerRequest{Slug: input.Slug, Name: input.Name, Command: input.Command, Args: input.Args, EnvKeys: input.EnvKeys})
		if err != nil {
			h.writeError(w, err)
			return
		}
		writeOK(w, contracts.McpServerMutationOutput{Server: mcpServer(resp.Server), WorkerRestartRequired: resp.WorkerRestartRequired})
	default:
		WriteMethodNotAllowed(w, "GET, POST")
	}
}
func (h *McpHandler) Item(w http.ResponseWriter, r *http.Request, id string) {
	if _, err := uuid.Parse(id); err != nil {
		writeError(w, 400, "VALIDATION_ERROR", "MCP server_id 格式无效", "validation", false)
		return
	}
	if r.Method != http.MethodPatch {
		WriteMethodNotAllowed(w, "PATCH")
		return
	}
	var input contracts.UpdateMcpServerInput
	if json.NewDecoder(r.Body).Decode(&input) != nil {
		writeError(w, 400, "VALIDATION_ERROR", "请求格式无效", "validation", false)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	resp, err := h.controlPlane.UpdateMcpServer(ctx, id, controlplane.UpdateMcpServerRequest{Enabled: input.Enabled, ExpectedVersion: input.ExpectedVersion})
	if err != nil {
		h.writeError(w, err)
		return
	}
	writeOK(w, contracts.McpServerMutationOutput{Server: mcpServer(resp.Server), WorkerRestartRequired: resp.WorkerRestartRequired})
}
func (h *McpHandler) Refresh(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		WriteMethodNotAllowed(w, "POST")
		return
	}
	if h.publisher == nil {
		writeError(w, 503, "MCP_DISCOVERY_UNAVAILABLE", "MCP 工具发现需要 Redis Worker", "runtime", true)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	commandID, err := h.publisher.RequestMcpDiscovery(ctx)
	if err != nil {
		writeError(w, 503, "MCP_DISCOVERY_COMMAND_FAILED", "MCP 工具发现请求暂时无法发送", "runtime", true)
		return
	}
	writeOK(w, contracts.McpDiscoveryRefreshOutput{
		CommandID: commandID, Status: "accepted", WorkerRestartRequired: true,
	})
}
func (h *McpHandler) writeError(w http.ResponseWriter, err error) {
	if e, ok := err.(*controlplane.ControlPlaneError); ok {
		st, code, msg, cat, rec := mapControlPlaneError(e)
		writeError(w, st, code, msg, cat, rec)
		return
	}
	writeError(w, 503, "MCP_SERVICE_ERROR", "MCP 服务暂不可用", "runtime", true)
}
func mcpList(v *controlplane.ListMcpServersResponse) contracts.ListMcpServersOutput {
	items := make([]contracts.McpServerDTO, len(v.Servers))
	for i, item := range v.Servers {
		items[i] = mcpServer(item)
	}
	return contracts.ListMcpServersOutput{Servers: items, WorkerRestartRequired: v.WorkerRestartRequired}
}
func mcpServer(v controlplane.McpServerDTO) contracts.McpServerDTO {
	tools := make([]contracts.McpToolDTO, len(v.Tools))
	for i, t := range v.Tools {
		tools[i] = contracts.McpToolDTO{ID: t.ID, OriginalName: t.OriginalName, InternalName: t.InternalName, Description: t.Description, InputSchema: t.InputSchema, RiskLevel: t.RiskLevel, Enabled: t.Enabled}
	}
	return contracts.McpServerDTO{ID: v.ID, Slug: v.Slug, Name: v.Name, Transport: v.Transport, Command: v.Command, Args: v.Args, EnvKeys: v.EnvKeys, Enabled: v.Enabled, Status: v.Status, LastErrorCode: v.LastErrorCode, LastConnectedAt: v.LastConnectedAt, Version: v.Version, CreatedAt: v.CreatedAt, UpdatedAt: v.UpdatedAt, Tools: tools}
}
