import { describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@jarvis/shared";
import { buildToolCallViews } from "@/features/inspector/composables/toolCallView";

const base = {
  task_id: "task-1",
  run_id: "run-1",
};

describe("buildToolCallViews", () => {
  it("merges persisted start and finish events into one readable tool call", () => {
    const events: RuntimeEvent[] = [
      {
        ...base,
        id: "event-start",
        type: "tool.call.started",
        timestamp: "2026-07-16T00:00:00.000Z",
        payload: {
          tool_call: {
            id: "call-1",
            tool_name: "workspace.read_file",
            provider: "native",
            risk_level: "L0",
            status: "running",
            arguments_summary: { path: "AGENTS.md", workspace_root: "/workspace" },
          },
        },
      },
      {
        ...base,
        id: "event-finish",
        type: "tool.call.finished",
        timestamp: "2026-07-16T00:00:00.125Z",
        payload: {
          tool_call: {
            id: "call-1",
            tool_name: "workspace.read_file",
            provider: "native",
            risk_level: "L0",
            status: "completed",
            result: { kind: "text", summary: "已读取 AGENTS.md" },
          },
          content_summary: { preview: "# AGENTS.md" },
        },
      },
    ];

    expect(buildToolCallViews(events)).toEqual([expect.objectContaining({
      id: "call-1",
      toolName: "workspace.read_file",
      status: "completed",
      durationMs: 125,
      resultSummary: "已读取 AGENTS.md",
      contentPreview: "# AGENTS.md",
      argumentsSummary: { path: "AGENTS.md", workspace_root: "/workspace" },
    })]);
  });

  it("preserves structured AppError for failed tools", () => {
    const events: RuntimeEvent[] = [{
      ...base,
      id: "event-failed",
      type: "tool.call.failed",
      timestamp: "2026-07-16T00:00:00Z",
      payload: {
        tool_call: {
          id: "call-2",
          tool_name: "workspace.read_file",
          status: "failed",
          error: {
            code: "FILE_NOT_FOUND",
            message: "文件不存在",
            category: "tool",
            recoverable: false,
          },
        },
      },
    }];

    expect(buildToolCallViews(events)[0]).toEqual(expect.objectContaining({
      status: "failed",
      error: expect.objectContaining({ code: "FILE_NOT_FOUND", category: "tool" }),
    }));
  });

  it("shows an expired permission as a terminal authorization fact", () => {
    const events: RuntimeEvent[] = [{
      ...base,
      id: "event-expired",
      type: "tool.call.failed",
      timestamp: "2026-08-03T00:00:00Z",
      payload: {
        tool_call: {
          id: "call-expired",
          tool_name: "workspace.create_file",
          status: "failed",
          permission_status: "expired",
          error: {
            code: "RUN_CANCELLED",
            message: "运行已取消",
            category: "runtime",
            recoverable: false,
          },
        },
      },
    }];

    expect(buildToolCallViews(events)[0]).toEqual(expect.objectContaining({
      status: "failed",
      permissionStatus: "expired",
    }));
  });
});
