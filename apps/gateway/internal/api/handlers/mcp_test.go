package handlers

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

type fakeMcpControlPlane struct {
	builtinCalls int
}

func (f *fakeMcpControlPlane) ListMcpServers(context.Context) (*controlplane.ListMcpServersResponse, error) {
	return &controlplane.ListMcpServersResponse{}, nil
}
func (f *fakeMcpControlPlane) CreateMcpServer(context.Context, controlplane.CreateMcpServerRequest) (*controlplane.McpServerResponse, error) {
	return nil, nil
}
func (f *fakeMcpControlPlane) UpdateMcpServer(context.Context, string, controlplane.UpdateMcpServerRequest) (*controlplane.McpServerResponse, error) {
	return nil, nil
}
func (f *fakeMcpControlPlane) ConnectBuiltinLiteratureServer(context.Context) (*controlplane.McpServerResponse, error) {
	f.builtinCalls++
	return &controlplane.McpServerResponse{
		Server:                controlplane.McpServerDTO{ID: "server-1", Slug: "jarvis_literature"},
		WorkerRestartRequired: true,
	}, nil
}

type fakeMcpDiscoveryPublisher struct {
	commandID string
	calls     int
}

func (f *fakeMcpDiscoveryPublisher) RequestMcpDiscovery(context.Context) (string, error) {
	f.calls++
	return f.commandID, nil
}

func TestMcpRefreshPublishesWorkerCommand(t *testing.T) {
	publisher := &fakeMcpDiscoveryPublisher{commandID: "mcp-command-1"}
	handler := NewMcpHandler(nil, publisher)
	recorder := httptest.NewRecorder()
	handler.Refresh(
		recorder,
		httptest.NewRequest(http.MethodPost, "/api/mcp-servers/refresh", nil),
	)
	var response contracts.ApiResult[contracts.McpDiscoveryRefreshOutput]
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if recorder.Code != http.StatusOK || response.Data == nil {
		t.Fatalf("unexpected response: %d %s", recorder.Code, recorder.Body.String())
	}
	if response.Data.Status != "accepted" || response.Data.CommandID != "mcp-command-1" {
		t.Fatalf("unexpected discovery response: %+v", response.Data)
	}
	if publisher.calls != 1 {
		t.Fatalf("publisher calls = %d", publisher.calls)
	}
}

func TestMcpRefreshFailsClosedWithoutRedisPublisher(t *testing.T) {
	recorder := httptest.NewRecorder()
	NewMcpHandler(nil, nil).Refresh(
		recorder,
		httptest.NewRequest(http.MethodPost, "/api/mcp-servers/refresh", nil),
	)
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d body=%s", recorder.Code, recorder.Body.String())
	}
}

func TestConnectBuiltinLiteratureDelegatesToControlPlane(t *testing.T) {
	cp := &fakeMcpControlPlane{}
	recorder := httptest.NewRecorder()
	NewMcpHandler(cp, nil).ConnectBuiltinLiterature(
		recorder,
		httptest.NewRequest(http.MethodPost, "/api/mcp-servers/builtin/literature", nil),
	)
	var response contracts.ApiResult[contracts.McpServerMutationOutput]
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if recorder.Code != http.StatusOK || response.Data == nil {
		t.Fatalf("unexpected response: %d %s", recorder.Code, recorder.Body.String())
	}
	if response.Data.Server.Slug != "jarvis_literature" || !response.Data.WorkerRestartRequired {
		t.Fatalf("unexpected built-in response: %+v", response.Data)
	}
	if cp.builtinCalls != 1 {
		t.Fatalf("builtin calls = %d", cp.builtinCalls)
	}
}
