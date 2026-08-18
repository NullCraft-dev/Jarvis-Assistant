import type { RuntimeEvent } from "@jarvis/shared";

type JsonRecord = Record<string, unknown>;

export type RunControlView = {
  eventId: string;
  title: string;
  summary: string;
  checkpointLabel: string;
  timestamp: string;
  state: "retry" | "paused" | "resumed";
};

const CHECKPOINT_LABELS: Record<string, string> = {
  extract_intent: "理解任务",
  call_model: "模型推理",
  validate_action: "动作校验",
  execute_tool: "工具执行",
};

/**
 * 将 Runtime 明确发布的恢复元数据转换为产品文案。
 * 未知节点只显示“安全检查点”，避免把内部图节点泄漏到普通界面或由前端猜测运行状态。
 */
export function getRunControlView(event: RuntimeEvent): RunControlView | null {
  const payload = asRecord(event.payload);
  const checkpointLabel = checkpointLabelFor(payload.resume_node);

  if (event.type === "agent.run.started" && payload.retry_from_checkpoint === true) {
    return {
      eventId: event.id,
      title: "重试运行已开始",
      summary: `将从${checkpointPhrase(checkpointLabel)}重新执行。`,
      checkpointLabel,
      timestamp: event.timestamp,
      state: "retry",
    };
  }

  if (event.type === "agent.run.paused") {
    return {
      eventId: event.id,
      title: "运行已暂停",
      summary: `已在「${checkpointLabel}」前保存状态，可以稍后继续。`,
      checkpointLabel,
      timestamp: event.timestamp,
      state: "paused",
    };
  }

  if (event.type === "agent.run.resumed") {
    return {
      eventId: event.id,
      title: "运行已恢复",
      summary: `已从${checkpointPhrase(checkpointLabel)}继续执行。`,
      checkpointLabel,
      timestamp: event.timestamp,
      state: "resumed",
    };
  }

  return null;
}

export function getLatestRunControlView(events: RuntimeEvent[]): RunControlView | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const view = getRunControlView(events[index]);
    if (view) return view;
  }
  return null;
}

function checkpointLabelFor(value: unknown): string {
  if (typeof value !== "string" || !value) return "安全检查点";
  return CHECKPOINT_LABELS[value] ?? "安全检查点";
}

function checkpointPhrase(label: string): string {
  return label === "安全检查点" ? label : `「${label}」安全检查点`;
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}
