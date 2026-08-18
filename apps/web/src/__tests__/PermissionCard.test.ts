// @vitest-environment happy-dom

import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PermissionRequestDTO } from "@jarvis/shared";
import PermissionCard from "@/features/timeline/components/PermissionCard.vue";

vi.mock("@/api/client", () => ({
  resolvePermission: vi.fn(),
  listPendingPermissions: vi.fn(),
}));

function request(overrides: Partial<PermissionRequestDTO> = {}): PermissionRequestDTO {
  return {
    id: "request-1",
    task_id: "task-1",
    run_id: "run-1",
    tool_name: "workspace.create_file",
    action_summary: "创建一份测试报告",
    reason: "需要把任务结果写入当前工作区",
    risk_level: "L2",
    scope: {
      type: "once",
      workspace_path: "/workspace",
      path: "notes/report.md",
    },
    arguments_summary: {
      path: "notes/report.md",
      content: {
        redacted: true,
        size_bytes: 42,
        sha256: "b".repeat(64),
      },
    },
    allowed_decisions: ["allow_once", "deny"],
    created_at: "2026-07-31T00:00:00Z",
    expires_at: "2099-07-31T00:15:00Z",
    status: "pending",
    ...overrides,
  };
}

describe("PermissionCard", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("shows impact scope, safe arguments and explicit decision duration", () => {
    const wrapper = mount(PermissionCard, { props: { request: request() } });

    expect(wrapper.text()).toContain("仅当前操作");
    expect(wrapper.text()).toContain("不会自动批准后续操作");
    expect(wrapper.text()).toContain("notes/report.md");
    expect(wrapper.text()).toContain("内容已脱敏");
    expect(wrapper.text()).toContain("允许本次操作");
    expect(wrapper.text()).toContain("拒绝操作");
    expect(wrapper.text()).not.toContain("\"redacted\"");
  });

  it("fails closed when an L4 request incorrectly declares persistent choices", () => {
    const wrapper = mount(PermissionCard, {
      props: {
        request: request({
          risk_level: "L4",
          allowed_decisions: [
            "allow_once",
            "always_allow_for_workspace",
            "deny",
          ],
        }),
      },
    });

    expect(wrapper.text()).toContain("允许本次操作");
    expect(wrapper.text()).toContain("拒绝操作");
    expect(wrapper.text()).not.toContain("持续允许当前工作区");
    expect(wrapper.text()).toContain("不能永久批准");
  });

  it("states that an accepted approval is not a successful tool result", () => {
    const wrapper = mount(PermissionCard, {
      props: {
        request: request({
          status: "approved",
          decision: "allow_once",
        }),
      },
    });

    expect(wrapper.text()).toContain("授权决定已受理");
    expect(wrapper.text()).toContain("工具是否成功仍以后续工具和运行事件为准");
    expect(wrapper.findAll("button")).toHaveLength(0);
  });

  it("fails closed locally after the durable deadline while awaiting server reconciliation", () => {
    const wrapper = mount(PermissionCard, {
      props: {
        request: request({
          expires_at: "2020-01-01T00:00:00Z",
        }),
      },
    });

    expect(wrapper.text()).toContain("授权请求已过期");
    expect(wrapper.text()).toContain("本次操作不会执行");
    expect(wrapper.findAll("button")).toHaveLength(0);
    expect(wrapper.attributes("aria-label")).toBe("已过期权限请求");
  });
});
