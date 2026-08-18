// @vitest-environment happy-dom

import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";
import type { RuntimeEvent, TaskDTO } from "@jarvis/shared";

import RightInspector from "@/features/inspector/components/RightInspector.vue";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useUiStore } from "@/stores/uiStore";

function task(): TaskDTO {
  return {
    id: "task-1",
    conversation_id: "conversation-1",
    title: "整理知识笔记",
    user_goal: "保存本周技术报告",
    status: "running",
    workspace_path: "/workspace/project",
    active_run_id: "run-1",
    created_at: "2026-07-27T00:00:00Z",
    updated_at: "2026-07-27T00:00:01Z",
  };
}

function contextEvent(overrides: Record<string, unknown> = {}): RuntimeEvent {
  return {
    id: "event-1",
    type: "model.context.prepared",
    task_id: "task-1",
    run_id: "run-1",
    timestamp: "2026-07-27T00:00:02Z",
    payload: {
      provider: "deepseek",
      model_name: "deepseek-chat",
      fingerprint: "context-fingerprint",
      policy_version: "context-v2-memory-v1-skill-v1",
      estimator: "heuristic-v1",
      estimated_input_tokens: 1200,
      input_budget_tokens: 8000,
      context_window_tokens: 16384,
      max_output_tokens: 4096,
      safety_margin_tokens: 1024,
      included_history_turns: 2,
      dropped_history_turns: 0,
      included_observations: 1,
      dropped_observations: 0,
      included_memories: 1,
      dropped_memories: 0,
      message_count: 5,
      truncated: false,
      ...overrides,
    },
  };
}

describe("RightInspector Skill observability", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
    const taskStore = useTaskStore();
    taskStore.tasks = [task()];
    taskStore.activeTaskId = "task-1";
    taskStore.activeRunId = "run-1";
  });

  it("shows the active Skill identity without exposing its instructions", () => {
    useRunStore().appendEvent(contextEvent({
      skill_id: "sample-advisor",
      skill_version: "1.0.0",
      skill_fingerprint: "a".repeat(64),
    }));

    const wrapper = mount(RightInspector);

    expect(wrapper.text()).toContain("已加载 Skill");
    expect(wrapper.text()).toContain("sample-advisor · v1.0.0");
    expect(wrapper.text()).toContain("a".repeat(64));
    expect(wrapper.text()).not.toContain("instructions");
  });

  it("omits the Skill block when the runtime did not activate one", () => {
    useRunStore().appendEvent(contextEvent());

    const wrapper = mount(RightInspector);

    expect(wrapper.text()).not.toContain("已加载 Skill");
  });

  it("does not repeat the user goal in the audit context layer", () => {
    const wrapper = mount(RightInspector);

    expect(wrapper.text()).toContain("运行约束");
    expect(wrapper.text()).toContain("工作区边界");
    expect(wrapper.text()).not.toContain("整理知识笔记");
    expect(wrapper.text()).not.toContain("保存本周技术报告");
  });

  it("shows the latest backend-confirmed recovery checkpoint in context", () => {
    const runStore = useRunStore();
    runStore.appendEvent({
      id: "run-paused",
      type: "agent.run.paused",
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-07-27T00:00:03Z",
      payload: { resume_node: "execute_tool" },
    });
    runStore.appendEvent({
      id: "run-resumed",
      type: "agent.run.resumed",
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-07-27T00:00:04Z",
      payload: { resume_node: "execute_tool" },
    });

    const wrapper = mount(RightInspector);
    const control = wrapper.get('[data-testid="run-control-view"]');

    expect(control.text()).toContain("运行已恢复");
    expect(control.text()).toContain("工具执行");
    expect(control.text()).not.toContain("execute_tool");
  });

  it("keeps raw event names and IDs inside the explicit technical layer", () => {
    useUiStore().setInspectorTab("logs");
    useRunStore().appendEvent({
      id: "event-run-failed",
      type: "agent.run.failed",
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-07-27T00:00:03Z",
      payload: {
        error: {
          code: "MODEL_TIMEOUT",
          message: "模型响应超时",
          category: "model",
          recoverable: true,
        },
      },
    });

    const wrapper = mount(RightInspector);

    expect(wrapper.text()).toContain("技术诊断层");
    expect(wrapper.text()).toContain("任务执行失败");
    expect(wrapper.text()).toContain("agent.run.failed");
    expect(wrapper.text()).toContain("event-run-failed");
  });

  it("shows only the latest permission state for one resolved request", () => {
    useUiStore().setInspectorTab("permissions");
    const runStore = useRunStore();
    runStore.appendEvent({
      id: "permission-required",
      type: "permission.required",
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-07-27T00:00:03Z",
      payload: {
        request: {
          id: "request-1",
          task_id: "task-1",
          run_id: "run-1",
          tool_name: "workspace.create_file",
          action_summary: "创建报告",
          reason: "保存结果",
          risk_level: "L2",
          scope: { type: "once", path: "report.md" },
          arguments_summary: { path: "report.md" },
          allowed_decisions: ["allow_once", "deny"],
          created_at: "2026-07-27T00:00:03Z",
          expires_at: "2099-07-27T00:15:03Z",
          status: "pending",
        },
      },
    });
    runStore.appendEvent({
      id: "permission-resolved",
      type: "permission.resolved",
      task_id: "task-1",
      run_id: "run-1",
      timestamp: "2026-07-27T00:00:04Z",
      payload: { request_id: "request-1", decision: "deny" },
    });

    const wrapper = mount(RightInspector);
    const text = wrapper.text();

    expect(text).toContain("拒绝操作");
    expect(text).not.toContain("等待用户确认");
    expect(text.match(/拒绝操作/g)).toHaveLength(1);
  });
});
