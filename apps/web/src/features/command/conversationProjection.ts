import type { AgentRunStatus } from "@jarvis/shared";

const TERMINAL_RUN_STATUSES = new Set<AgentRunStatus>([
  "completed",
  "failed",
  "cancelled",
]);

/**
 * A live draft can cover a persisted assistant message only while the run is
 * non-terminal. Once the run finishes, Storage is the conversation truth and
 * must not be shadowed by a partial SSE/typewriter buffer.
 */
export function shouldProjectLiveRunText(
  runStatus: AgentRunStatus,
  hasLiveText: boolean,
): boolean {
  return hasLiveText && !TERMINAL_RUN_STATUSES.has(runStatus);
}
