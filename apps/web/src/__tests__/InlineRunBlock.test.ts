// @vitest-environment happy-dom

import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@jarvis/shared";

import InlineRunBlock from "@/features/timeline/components/InlineRunBlock.vue";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";

function event(
  id: string,
  type: RuntimeEvent["type"],
  payload: Record<string, unknown> = {},
): RuntimeEvent {
  return {
    id,
    type,
    task_id: "task-1",
    run_id: "run-1",
    timestamp: "2026-07-31T00:00:00Z",
    payload,
  };
}

describe("InlineRunBlock information hierarchy", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("collapses terminal runs and reports key nodes instead of raw event count", async () => {
    const runStore = useRunStore();
    runStore.appendEvent(event("started", "agent.run.started"));
    runStore.appendEvent(event("delta", "model.delta", { delta: "正文只应出现在对话里" }));
    runStore.appendEvent(event("completed", "agent.run.completed", { total_steps: 1 }));

    const wrapper = mount(InlineRunBlock, { props: { runId: "run-1" } });

    expect(wrapper.text()).toContain("执行过程");
    expect(wrapper.text()).toContain("已完成");
    expect(wrapper.text()).toContain("2 个关键节点");
    expect(wrapper.text()).not.toContain("3 个事件");
    expect(wrapper.text()).not.toContain("正文只应出现在对话里");
    expect(wrapper.text()).not.toContain("completed");

    await wrapper.get("button").trigger("click");
    expect(wrapper.text()).toContain("开始执行任务");
    expect(wrapper.text()).toContain("任务执行完成");
    expect(wrapper.text()).not.toContain("正在生成回复");
  });

  it("auto-expands a new local run and returns focus to the result at terminal state", async () => {
    const taskStore = useTaskStore();
    taskStore.localPresentationRunId = "run-1";
    const runStore = useRunStore();
    runStore.appendEvent(event("started", "agent.run.started"));

    const wrapper = mount(InlineRunBlock, { props: { runId: "run-1" } });
    expect(wrapper.text()).toContain("开始执行任务");

    runStore.appendEvent(event("completed", "agent.run.completed"));
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain("2 个关键节点");
    expect(wrapper.text()).not.toContain("Agent 已接手任务并开始规划执行");
  });
});
