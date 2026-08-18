import type { AgentRunStatus, ID, RuntimeEvent } from "@jarvis/shared";

/**
 * 只在终态错误与最新模型失败都声明可恢复时展示重试入口。
 * 后端仍会再次校验 PostgreSQL checkpoint；前端不从更早的失败事件猜测能力。
 */
export function findRetryableModelStepId(
  status: AgentRunStatus | null,
  events: RuntimeEvent[],
): ID | null {
  if (status !== "failed") return null;
  const terminal = [...events].reverse().find((event) => event.type === "agent.run.failed");
  const terminalError = (terminal?.payload as { error?: { recoverable?: unknown } } | undefined)?.error;
  if (terminalError?.recoverable !== true) return null;

  const latestModelFailure = [...events]
    .reverse()
    .find((event) => event.type === "model.call.failed");
  if (!latestModelFailure?.step_id) return null;
  const payload = latestModelFailure.payload as { recoverable?: unknown };
  return payload.recoverable === true ? latestModelFailure.step_id : null;
}
