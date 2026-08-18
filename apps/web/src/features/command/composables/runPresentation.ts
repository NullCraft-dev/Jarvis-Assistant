import type { AgentRunStatus, AppError, RuntimeEvent } from "@jarvis/shared";

export type RunTone = "neutral" | "info" | "warning" | "success" | "danger";

export type RunStatusPresentation = {
  label: string;
  description: string;
  tone: RunTone;
};

const RUN_STATUS_PRESENTATIONS: Record<AgentRunStatus, RunStatusPresentation> = {
  created: {
    label: "正在创建",
    description: "任务已接收，正在准备运行环境。",
    tone: "neutral",
  },
  queued: {
    label: "排队中",
    description: "任务已进入队列，正在等待可用 Worker。",
    tone: "neutral",
  },
  running: {
    label: "运行中",
    description: "Agent 正在执行任务，进度会通过事件持续更新。",
    tone: "info",
  },
  pause_requested: {
    label: "正在安全暂停",
    description: "暂停请求已提交，当前步骤收口后会停止。",
    tone: "warning",
  },
  paused: {
    label: "已暂停",
    description: "运行已安全暂停，可以继续或取消。",
    tone: "warning",
  },
  resume_requested: {
    label: "正在恢复",
    description: "恢复请求已提交，正在等待 Worker 继续执行。",
    tone: "info",
  },
  waiting_for_permission: {
    label: "等待授权",
    description: "Agent 需要你的确认才能继续执行下一项操作。",
    tone: "warning",
  },
  waiting_for_user: {
    label: "等待补充信息",
    description: "Agent 需要更多信息，请在对话中回复。",
    tone: "warning",
  },
  blocked: {
    label: "运行受阻",
    description: "当前运行无法继续，请查看错误信息或检查服务状态。",
    tone: "danger",
  },
  failed: {
    label: "运行失败",
    description: "运行已停止。请查看原因和可用的恢复方式。",
    tone: "danger",
  },
  completed: {
    label: "已完成",
    description: "任务已完成，结果和交付物已保存。",
    tone: "success",
  },
  cancelled: {
    label: "已取消",
    description: "运行已取消，已产生的记录和交付物仍会保留。",
    tone: "neutral",
  },
};

export function getRunStatusPresentation(status: AgentRunStatus): RunStatusPresentation {
  return RUN_STATUS_PRESENTATIONS[status];
}

export function isActiveRunStatus(status: AgentRunStatus | null): boolean {
  return status !== null && !["failed", "completed", "cancelled"].includes(status);
}

export function getLatestRunError(events: RuntimeEvent[]): AppError | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.type !== "agent.run.failed") continue;
    const candidate = (event.payload as { error?: unknown })?.error;
    if (!candidate || typeof candidate !== "object") return null;
    const error = candidate as Partial<AppError>;
    if (
      typeof error.code === "string" &&
      typeof error.message === "string" &&
      typeof error.category === "string" &&
      typeof error.recoverable === "boolean"
    ) {
      return error as AppError;
    }
    return null;
  }
  return null;
}
