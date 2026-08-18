import { describe, expect, it } from "vitest";
import type { RuntimeEvent } from "@jarvis/shared";
import {
  buildTimelineEvents,
  getRuntimeEventPresentation,
  summarizeTimeline,
} from "@/features/timeline/runtimeEventPresentation";

function event(
  id: string,
  type: RuntimeEvent["type"],
  payload: Record<string, unknown> = {},
  stepId?: string,
): RuntimeEvent {
  return {
    id,
    type,
    task_id: "task-1",
    run_id: "run-1",
    step_id: stepId,
    timestamp: "2026-07-31T00:00:00Z",
    payload,
  };
}

describe("runtime event presentation", () => {
  it("keeps only user-relevant milestones and folds completed lifecycles", () => {
    const events: RuntimeEvent[] = [
      event("run-start", "agent.run.started"),
      event("context", "model.context.prepared", {
        estimated_input_tokens: 1200,
        input_budget_tokens: 8000,
      }),
      event("model-start", "model.call.started", { call_id: "model-1" }, "step-model"),
      event("delta", "model.delta", { delta: "secret streamed text" }, "step-model"),
      event("model-end", "model.call.completed", {
        call_id: "model-1",
        action_type: "tool_call",
        duration_ms: 250,
      }, "step-model"),
      event("tool-start", "tool.call.started", {
        tool_call: { id: "tool-1", tool_name: "workspace.read_file" },
      }, "step-tool"),
      event("tool-end", "tool.call.finished", {
        tool_call: {
          id: "tool-1",
          tool_name: "workspace.read_file",
          result: { summary: "已读取文件" },
        },
      }, "step-tool"),
      event("final-artifact", "artifact.created", {
        artifact: { id: "artifact-1", purpose: "final_response", title: "最终回复" },
      }),
      event("run-end", "agent.run.completed", { total_steps: 2 }),
    ];

    expect(buildTimelineEvents(events).map((item) => item.id)).toEqual([
      "run-start",
      "model-end",
      "tool-end",
      "run-end",
    ]);
    expect(summarizeTimeline(events)).toBe("4 个关键节点 · 1 个工具节点");
  });

  it("uses user-facing labels and only safe failure fields", () => {
    const presentation = getRuntimeEventPresentation(event("failed", "agent.run.failed", {
      error: {
        code: "MODEL_TIMEOUT",
        message: "模型响应超时",
        details: { authorization: "must-not-render" },
      },
    }));

    expect(presentation.title).toBe("任务执行失败");
    expect(presentation.summary).toContain("模型响应超时（MODEL_TIMEOUT）");
    expect(presentation.summary).not.toContain("authorization");
    expect(presentation.summary).not.toContain("must-not-render");
    expect(presentation.detailTarget).toBe("logs");
  });

  it("maps retry and resume checkpoints without exposing unknown graph nodes", () => {
    const retry = getRuntimeEventPresentation(event("retry", "agent.run.started", {
      retry_from_checkpoint: true,
      resume_node: "call_model",
    }));
    const resumed = getRuntimeEventPresentation(event("resumed", "agent.run.resumed", {
      resume_node: "future_internal_node",
    }));

    expect(retry.title).toBe("重试运行已开始");
    expect(retry.summary).toContain("模型推理");
    expect(resumed.summary).toContain("安全检查点");
    expect(resumed.summary).not.toContain("future_internal_node");
  });

  it("keeps deliverable artifacts as process evidence", () => {
    const deliverable = event("artifact", "artifact.created", {
      artifact: {
        id: "artifact-2",
        purpose: "deliverable",
        title: "分析报告",
        kind: "markdown",
      },
    });

    expect(buildTimelineEvents([deliverable])).toEqual([deliverable]);
    expect(getRuntimeEventPresentation(deliverable)).toEqual(expect.objectContaining({
      title: "分析报告",
      categoryLabel: "交付物",
    }));
  });

  it("replaces a settled permission request with its latest state", () => {
    const required = event("required", "permission.required", {
      request: {
        id: "request-1",
        action_summary: "创建报告",
        risk_level: "L2",
      },
    });
    const resolved = event("resolved", "permission.resolved", {
      request_id: "request-1",
      decision: "deny",
    });

    expect(buildTimelineEvents([required, resolved])).toEqual([resolved]);
    expect(summarizeTimeline([required, resolved])).toBe("1 个关键节点 · 1 个权限节点");
  });
});
