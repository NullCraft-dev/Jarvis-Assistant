import { describe, expect, it } from "vitest";
import type { PermissionDecisionType } from "@jarvis/shared";
import {
  formatPermissionArguments,
  getPermissionDecisionPresentation,
  getPermissionScopePresentation,
} from "@/features/permission/permissionPresentation";

describe("permission presentation", () => {
  it("explains every contracted permission decision", () => {
    const decisions: PermissionDecisionType[] = [
      "allow_once",
      "allow_for_task",
      "always_allow_for_tool_and_path",
      "always_allow_for_workspace",
      "deny",
    ];

    for (const decision of decisions) {
      const presentation = getPermissionDecisionPresentation(decision);
      expect(presentation.label).not.toBe(decision);
      expect(presentation.description.length).toBeGreaterThan(10);
    }
  });

  it("turns a raw once scope into an explicit impact explanation", () => {
    expect(getPermissionScopePresentation({
      type: "once",
      workspace_path: "/workspace",
      path: "notes/report.md",
    })).toEqual({
      label: "仅当前操作",
      description: "批准只对这一次权限请求有效，不会自动批准后续操作。",
      facts: [
        { label: "工作区", value: "/workspace" },
        { label: "路径", value: "notes/report.md" },
      ],
    });
  });

  it("redacts sensitive values and renders content fingerprints without raw content", () => {
    const rows = formatPermissionArguments({
      path: "notes/report.md",
      content: {
        redacted: true,
        size_bytes: 1536,
        sha256: "a".repeat(64),
      },
      api_key: "must-not-render",
    });

    expect(rows).toContainEqual(expect.objectContaining({
      label: "目标路径",
      value: "notes/report.md",
    }));
    expect(rows).toContainEqual(expect.objectContaining({
      key: "content",
      value: expect.stringContaining("内容已脱敏 · 1.5 KiB · SHA-256"),
    }));
    expect(rows).toContainEqual(expect.objectContaining({
      key: "api_key",
      value: "已隐藏敏感值",
    }));
    expect(JSON.stringify(rows)).not.toContain("must-not-render");
  });
});
