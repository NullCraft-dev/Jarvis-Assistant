// @vitest-environment happy-dom

import { mount, flushPromises } from "@vue/test-utils";
import { createPinia } from "pinia";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getSettings: vi.fn(),
  getWorkers: vi.fn(),
  listConversations: vi.fn(),
  listTasks: vi.fn(),
  listPendingPermissions: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  ...apiMocks,
  createTask: vi.fn(),
}));

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn() }),
  useRoute: () => ({ path: "/" }),
}));

import AppSidebar from "@/components/layout/AppSidebar.vue";

describe("AppSidebar initialization", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    apiMocks.getSettings.mockResolvedValue({
      ok: true,
      data: {
        model: { fallback_enabled: false, api_key_configured: true },
        workspace: {
          default_workspace_path: "/workspace",
          allowed_workspace_paths: ["/workspace"],
        },
        permissions: { default_shell_policy: "confirm", high_risk_policy: "always_confirm" },
        mcp: { servers: [] },
        runtime: {
          storage_backend: "postgresql",
          persistence_status: "ready",
          runtime_bus: "redis",
          control_plane_status: "ready",
        },
      },
    });
    apiMocks.getWorkers.mockResolvedValue({ ok: true, data: { workers: [] } });
    apiMocks.listTasks.mockResolvedValue({ ok: true, data: { tasks: [] } });
    apiMocks.listPendingPermissions.mockResolvedValue({ ok: true, data: { requests: [] } });
    apiMocks.listConversations.mockResolvedValue({
      ok: true,
      data: {
        conversations: [{
          id: "conv-1",
          title: "测试会话",
          created_at: "2026-07-15T00:00:00Z",
          updated_at: "2026-07-15T00:00:00Z",
        }],
      },
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  it("independently initializes settings, conversations, tasks, and workers", async () => {
    const wrapper = mount(AppSidebar, {
      global: { plugins: [createPinia()] },
    });
    await flushPromises();

    expect(apiMocks.getSettings).toHaveBeenCalledOnce();
    expect(apiMocks.listConversations).toHaveBeenCalledOnce();
    expect(apiMocks.listTasks).toHaveBeenCalledOnce();
    expect(apiMocks.getWorkers).toHaveBeenCalledOnce();
    expect(wrapper.text()).toContain("测试会话");
    expect(wrapper.text()).toContain("PostgreSQL");

    wrapper.unmount();
  });

  it("shows a retryable conversation error without blocking other initialization", async () => {
    apiMocks.listConversations.mockRejectedValueOnce(new Error("offline"));
    const wrapper = mount(AppSidebar, {
      global: { plugins: [createPinia()] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("网络异常，请重试");
    expect(apiMocks.listTasks).toHaveBeenCalledOnce();
    expect(apiMocks.getWorkers).toHaveBeenCalledOnce();

    wrapper.unmount();
  });

  it("uses the configured agent model when a model-less RAG worker is listed first", async () => {
    apiMocks.getWorkers.mockResolvedValueOnce({
      ok: true,
      data: {
        workers: [
          {
            worker_id: "rag-worker-01",
            status: "idle",
            active_run_id: "",
            reported_at: "2026-07-27T12:00:00Z",
            last_seen_at: "2026-07-27T12:00:00Z",
            is_stale: false,
          },
          {
            worker_id: "worker-01",
            status: "idle",
            active_run_id: "",
            reported_at: "2026-07-27T12:00:00Z",
            last_seen_at: "2026-07-27T12:00:00Z",
            is_stale: false,
            model: {
              provider: "deepseek",
              protocol: "openai_chat_completions",
              model_name: "deepseek-v4-flash",
              api_key_configured: true,
              thinking_mode: "disabled",
              status: "configured",
              last_error_code: null,
            },
          },
        ],
      },
    });
    const wrapper = mount(AppSidebar, {
      global: { plugins: [createPinia()] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("deepseek-v4-flash");
    expect(wrapper.text()).not.toContain("Model 未配置");

    wrapper.unmount();
  });
});
