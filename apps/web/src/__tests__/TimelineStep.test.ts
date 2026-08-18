// @vitest-environment happy-dom

import { mount } from "@vue/test-utils";
import type { RuntimeEvent } from "@jarvis/shared";
import { describe, expect, it } from "vitest";

import TimelineStep from "@/features/timeline/components/TimelineStep.vue";

function event(type: RuntimeEvent["type"], payload: Record<string, unknown>): RuntimeEvent {
  return {
    id: `event-${type}`,
    type,
    task_id: "task-1",
    run_id: "run-1",
    timestamp: "2026-07-28T00:00:00Z",
    payload,
  } as RuntimeEvent;
}

describe("TimelineStep final output summaries", () => {
  it("does not duplicate model delta content", () => {
    const wrapper = mount(TimelineStep, {
      props: { event: event("model.delta", { delta: '{"status":"ok"}' }) },
    });

    expect(wrapper.text()).toContain("回复已新增 15 个字符");
    expect(wrapper.text()).not.toContain("status");
  });

  it("does not duplicate completed output", () => {
    const wrapper = mount(TimelineStep, {
      props: {
        event: event("agent.run.completed", {
          output: "**不应在时间线重复显示**",
          total_steps: 2,
        }),
      },
    });

    expect(wrapper.text()).toContain("结果已验证并保存，共完成 2 个步骤");
    expect(wrapper.text()).not.toContain("不应在时间线重复显示");
  });
});
