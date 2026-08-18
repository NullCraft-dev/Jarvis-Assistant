// @vitest-environment happy-dom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import CommandRunStatus from "@/features/command/components/CommandRunStatus.vue";

describe("CommandRunStatus", () => {
  it("keeps long safe errors and recovery actions in a responsive action row", () => {
    const longMessage = `恢复失败：${"安全错误说明".repeat(40)}`;
    const wrapper = mount(CommandRunStatus, {
      props: {
        presentation: {
          label: "运行失败",
          description: "运行已停止。请查看原因和可用的恢复方式。",
          tone: "danger",
        },
        connectionState: "closed",
        runError: {
          code: "MODEL_PROVIDER_UNAVAILABLE",
          message: longMessage,
          category: "model",
          recoverable: true,
        },
        canRetryStep: true,
        canReconnect: false,
      },
    });

    expect(wrapper.text()).toContain(longMessage);
    expect(wrapper.text()).toContain("从安全检查点恢复");
    expect(wrapper.get("section > div").classes()).toContain("flex-col");
    expect(wrapper.get("section > div > div:last-child").classes()).toContain("w-full");
  });

  it("keeps reconnect visible when the event stream is closed", () => {
    const wrapper = mount(CommandRunStatus, {
      props: {
        presentation: {
          label: "运行中",
          description: "Agent 正在执行任务。",
          tone: "info",
        },
        connectionState: "closed",
        canReconnect: true,
      },
    });

    expect(wrapper.get("button").text()).toBe("重新连接");
  });
});
