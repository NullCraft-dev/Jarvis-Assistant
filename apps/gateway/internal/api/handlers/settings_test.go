package handlers

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jarvis-assistant/gateway/internal/contracts"
	"github.com/jarvis-assistant/gateway/internal/controlplane"
)

func TestGetSettingsDoesNotOwnModelProviderSemantics(t *testing.T) {
	t.Setenv("JARVIS_MODEL_PROVIDER", "openai_compatible")
	t.Setenv("JARVIS_MODEL_NAME", "deepseek-test")
	t.Setenv("JARVIS_WORKSPACE_ROOT", "/tmp/jarvis-workspace")
	t.Setenv("JARVIS_ALLOWED_WORKSPACE_PATHS", "")

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/settings", nil)
	NewSettingsHandler(nil).GetSettings(recorder, request)

	var response struct {
		Ok   bool                  `json:"ok"`
		Data contracts.SettingsDTO `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("解析 settings 响应失败: %v", err)
	}
	if !response.Ok {
		t.Fatal("settings 响应应为 ok")
	}
	if response.Data.Model.CloudProvider != "" || response.Data.Model.DefaultModel != "" {
		t.Errorf("未连接 Control Plane 时 Gateway 不应自行解释 Provider: %#v", response.Data.Model)
	}
	if response.Data.Workspace.DefaultWorkspacePath != "/tmp/jarvis-workspace" {
		t.Errorf("workspace 应来自环境变量，实际 %q", response.Data.Workspace.DefaultWorkspacePath)
	}
	if len(response.Data.Workspace.AllowedWorkspacePaths) != 1 ||
		response.Data.Workspace.AllowedWorkspacePaths[0] != "/tmp/jarvis-workspace" {
		t.Errorf("allowed workspace 应来自环境变量，实际 %#v", response.Data.Workspace.AllowedWorkspacePaths)
	}
}

func TestGetSettingsUsesControlPlaneModelProjection(t *testing.T) {
	httpClient := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		body := ""
		switch r.URL.Path {
		case "/internal/health":
			body = `{"ok":true,"data":{"status":"ok","database":"connected","redis":"connected","outbox_publisher":"running"}}`
		case "/internal/model-config":
			body = `{"ok":true,"data":{"provider":"deepseek","protocol":"openai_chat_completions","model_name":"deepseek-chat","api_key_configured":true}}`
		default:
			return &http.Response{
				StatusCode: http.StatusNotFound,
				Body:       io.NopCloser(strings.NewReader(`{"ok":false}`)),
				Header:     make(http.Header),
			}, nil
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Body:       io.NopCloser(strings.NewReader(body)),
			Header:     make(http.Header),
		}, nil
	})}

	handler := NewSettingsHandler(controlplane.NewClientWithHTTPClient("http://control-plane.invalid", httpClient))
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/settings", nil)
	handler.GetSettings(recorder, request)

	var response struct {
		Data contracts.SettingsDTO `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("解析 settings 响应失败: %v", err)
	}
	if response.Data.Model.CloudProvider != "deepseek" {
		t.Fatalf("provider 应来自 Control Plane，实际 %q", response.Data.Model.CloudProvider)
	}
	if response.Data.Model.DefaultModel != "deepseek-chat" {
		t.Fatalf("model 应来自 Control Plane，实际 %q", response.Data.Model.DefaultModel)
	}
	if !response.Data.Model.APIKeyConfigured {
		t.Fatal("API key 配置状态应来自 Control Plane")
	}
}

func TestGetSettingsHasNoMachineSpecificFallbacks(t *testing.T) {
	t.Setenv("JARVIS_MODEL_PROVIDER", "")
	t.Setenv("JARVIS_MODEL_NAME", "")
	t.Setenv("JARVIS_WORKSPACE_ROOT", "")
	t.Setenv("JARVIS_ALLOWED_WORKSPACE_PATHS", "")

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/settings", nil)
	NewSettingsHandler(nil).GetSettings(recorder, request)

	var response struct {
		Data contracts.SettingsDTO `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("解析 settings 响应失败: %v", err)
	}
	if response.Data.Model.CloudProvider != "" || response.Data.Model.DefaultModel != "" {
		t.Errorf("未配置时不应伪造模型信息: %#v", response.Data.Model)
	}
	if response.Data.Workspace.DefaultWorkspacePath != "" || len(response.Data.Workspace.AllowedWorkspacePaths) != 0 {
		t.Errorf("未配置时不应伪造 workspace: %#v", response.Data.Workspace)
	}
}

func TestGetSettingsReturnsDeduplicatedAllowedWorkspacePaths(t *testing.T) {
	t.Setenv("JARVIS_WORKSPACE_ROOT", "/tmp/jarvis-workspace")
	t.Setenv(
		"JARVIS_ALLOWED_WORKSPACE_PATHS",
		"/tmp/jarvis-workspace"+string(filepath.ListSeparator)+"/tmp/another-workspace",
	)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/api/settings", nil)
	NewSettingsHandler(nil).GetSettings(recorder, request)

	var response struct {
		Data contracts.SettingsDTO `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("解析 settings 响应失败: %v", err)
	}
	paths := response.Data.Workspace.AllowedWorkspacePaths
	if len(paths) != 2 || paths[0] != "/tmp/jarvis-workspace" || paths[1] != "/tmp/another-workspace" {
		t.Fatalf("允许工作区未正确解析/去重: %#v", paths)
	}
}
