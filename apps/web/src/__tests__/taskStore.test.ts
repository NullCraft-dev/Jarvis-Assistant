// @vitest-environment happy-dom

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeEvent, TaskDTO } from "@jarvis/shared";

const apiMocks = vi.hoisted(() => ({
  createTask: vi.fn(),
  listTasks: vi.fn(),
  listConversations: vi.fn(),
  getConversation: vi.fn(),
  listPendingPermissions: vi.fn(),
  subscribeRunEvents: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  ...apiMocks,
}));

import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useSettingsStore } from "@/stores/settingsStore";

function task(overrides: Partial<TaskDTO> = {}): TaskDTO {
  return {
    id: "task-1",
    conversation_id: "conv-1",
    title: "测试任务",
    user_goal: "测试目标",
    status: "failed",
    active_run_id: "run-1",
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T00:00:01Z",
    ...overrides,
  };
}

describe("taskStore conversation run restoration", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
    vi.clearAllMocks();
    apiMocks.listConversations.mockResolvedValue({
      ok: true,
      data: { conversations: [] },
    });
    apiMocks.getConversation.mockResolvedValue({
      ok: true,
      data: {
        conversation: {
          id: "conv-1", title: "测试会话",
          created_at: "2026-07-15T00:00:00Z", updated_at: "2026-07-15T00:00:01Z",
        },
        messages: [],
      },
    });
    apiMocks.listPendingPermissions.mockResolvedValue({
      ok: true,
      data: { requests: [] },
    });
  });

  it("restores the latest persisted run for the saved conversation", async () => {
    let eventHandler: ((event: RuntimeEvent) => void) | undefined;
    apiMocks.subscribeRunEvents.mockImplementation((_runId, handler) => {
      eventHandler = handler;
      return vi.fn();
    });
    apiMocks.listTasks.mockResolvedValue({
      ok: true,
      data: {
        tasks: [
          task({ id: "task-old", active_run_id: "run-old", updated_at: "2026-07-15T00:00:00Z" }),
          task(),
        ],
      },
    });
    localStorage.setItem("jarvis_active_conversation_id", "conv-1");

    const taskStore = useTaskStore();
    const runStore = useRunStore();
    await taskStore.restoreConversation();
    await taskStore.loadTasks();

    expect(taskStore.activeTaskId).toBe("task-1");
    expect(taskStore.activeRunId).toBe("run-1");
    expect(taskStore.localPresentationRunId).toBeNull();
    expect(apiMocks.subscribeRunEvents).toHaveBeenCalledWith(
      "run-1", expect.any(Function), expect.any(Function)
    );

    eventHandler?.({
      id: "event-failed",
      type: "agent.run.failed",
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-07-15T00:00:02Z",
      payload: {
        error: {
          code: "MODEL_OUTPUT_INVALID",
          message: "模型调用失败",
          category: "model",
          recoverable: false,
        },
      },
    });

    expect(runStore.getStatus("run-1")).toBe("failed");
    expect(runStore.getEvents("run-1")).toHaveLength(1);
  });

  it("drops a stale cached conversation before it can affect the first submit", async () => {
    localStorage.setItem("jarvis_active_conversation_id", "conv-stale");
    apiMocks.getConversation.mockResolvedValue({
      ok: false,
      error: {
        code: "NOT_FOUND",
        message: "会话不存在",
        category: "not_found",
        recoverable: false,
      },
    });

    const taskStore = useTaskStore();
    await taskStore.restoreConversation();

    expect(taskStore.activeConversationId).toBeNull();
    expect(localStorage.getItem("jarvis_active_conversation_id")).toBeNull();
  });

  it("retries once without a conversation when it disappears before submit", async () => {
    const taskStore = useTaskStore();
    taskStore.tasks = [task()];
    taskStore.selectConversation("conv-1");

    apiMocks.createTask
      .mockResolvedValueOnce({
        ok: false,
        error: {
          code: "NOT_FOUND",
          message: "会话不存在",
          category: "not_found",
          recoverable: false,
        },
      })
      .mockResolvedValueOnce({
        ok: true,
        data: {
          task: task({ status: "running", conversation_id: "conv-new" }),
          run: {
            id: "run-new", task_id: "task-1", agent_id: "default",
            mode: "single_agent", status: "queued",
            created_at: "2026-07-15T00:00:00Z", updated_at: "2026-07-15T00:00:00Z",
          },
          conversation: {
            id: "conv-new", title: "新会话",
            created_at: "2026-07-15T00:00:00Z", updated_at: "2026-07-15T00:00:00Z",
          },
          message: {
            id: "msg-new", conversation_id: "conv-new", task_id: "task-1",
            role: "user", content: "继续执行", created_at: "2026-07-15T00:00:00Z",
          },
        },
      });

    const result = await taskStore.createTask("继续执行");

    expect(result.ok).toBe(true);
    expect(apiMocks.createTask).toHaveBeenNthCalledWith(1, {
      user_goal: "继续执行",
      conversation_id: "conv-1",
      workspace_id: undefined,
    });
    expect(apiMocks.createTask).toHaveBeenNthCalledWith(2, {
      user_goal: "继续执行",
      workspace_id: undefined,
    });
    expect(taskStore.activeConversationId).toBe("conv-new");
  });

  it("selects and subscribes the latest task when opening a conversation", () => {
    apiMocks.subscribeRunEvents.mockReturnValue(vi.fn());
    const taskStore = useTaskStore();
    taskStore.tasks = [
      task({ id: "task-old", active_run_id: "run-old", updated_at: "2026-07-15T00:00:00Z" }),
      task({ id: "task-new", active_run_id: "run-new", updated_at: "2026-07-15T00:00:03Z" }),
    ];

    taskStore.selectConversation("conv-1");

    expect(taskStore.activeTaskId).toBe("task-new");
    expect(taskStore.activeRunId).toBe("run-new");
    expect(apiMocks.subscribeRunEvents).toHaveBeenCalledWith(
      "run-new", expect.any(Function), expect.any(Function)
    );
  });

  it("activates a replacement run after a safe failed-step retry", () => {
    apiMocks.subscribeRunEvents.mockReturnValue(vi.fn());
    const taskStore = useTaskStore();
    taskStore.tasks = [task()];
    taskStore.selectTask("task-1");

    taskStore.activateReplacementRun("run-replacement");

    expect(taskStore.activeRunId).toBe("run-replacement");
    expect(taskStore.localPresentationRunId).toBe("run-replacement");
    expect(taskStore.tasks[0].active_run_id).toBe("run-replacement");
    expect(taskStore.tasks[0].status).toBe("running");
    expect(apiMocks.subscribeRunEvents).toHaveBeenCalledWith(
      "run-replacement", expect.any(Function), expect.any(Function)
    );
  });

  it("passes only the selected server-allowed workspace when creating a task", async () => {
    const settingsStore = useSettingsStore();
    settingsStore.settings = {
      model: { fallback_enabled: false, api_key_configured: true },
      workspace: {
        default_workspace_path: "/workspace-a",
        allowed_workspace_paths: ["/workspace-a", "/workspace-b"],
      },
      permissions: { default_shell_policy: "confirm", high_risk_policy: "always_confirm" },
      mcp: { servers: [] },
      runtime: {
        storage_backend: "postgresql",
        persistence_status: "ready",
        runtime_bus: "redis",
        control_plane_status: "ready",
      },
    };
    // 使用 workspaceStore 设置选择的 workspace_id
    const { useWorkspaceStore } = await import("@/stores/workspaceStore");
    const workspaceStore = useWorkspaceStore();
    // 模拟 active workspace 列表
    workspaceStore.workspaces = [
      { id: "ws-1", name: "project-a", root_path: "/workspace-a", canonical_path: "/workspace-a", status: "active", source: "configured", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
      { id: "ws-2", name: "project-b", root_path: "/workspace-b", canonical_path: "/workspace-b", status: "active", source: "user_picker", created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" },
    ];
    expect(workspaceStore.setSelectedWorkspaceId("ws-2")).toBe(true);

    apiMocks.createTask.mockResolvedValue({
      ok: true,
      data: {
        task: task({ status: "running", workspace_path: "/workspace-b", workspace_id: "ws-2" }),
        run: {
          id: "run-1", task_id: "task-1", agent_id: "default",
          mode: "single_agent", status: "queued",
          created_at: "2026-07-15T00:00:00Z", updated_at: "2026-07-15T00:00:00Z",
        },
        conversation: {
          id: "conv-1", title: "测试任务",
          created_at: "2026-07-15T00:00:00Z", updated_at: "2026-07-15T00:00:00Z",
        },
        message: {
          id: "msg-1", conversation_id: "conv-1", task_id: "task-1",
          role: "user", content: "读取文件", created_at: "2026-07-15T00:00:00Z",
        },
      },
    });
    apiMocks.listConversations.mockResolvedValue({ ok: true, data: { conversations: [] } });

    const taskStore = useTaskStore();
    await taskStore.createTask("读取文件");

    expect(apiMocks.createTask).toHaveBeenCalledWith({
      user_goal: "读取文件",
      conversation_id: undefined,
      workspace_id: "ws-2",
    });
    expect(taskStore.localPresentationRunId).toBe("run-1");
  });
});
