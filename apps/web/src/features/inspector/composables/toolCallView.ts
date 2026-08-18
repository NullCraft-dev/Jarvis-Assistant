import type { AppError, RiskLevel, RuntimeEvent } from "@jarvis/shared";

type JsonRecord = Record<string, unknown>;

export type ToolCallView = {
  id: string;
  toolName: string;
  provider: string;
  riskLevel: RiskLevel;
  status: "running" | "completed" | "failed";
  permissionStatus: "not_required" | "pending" | "approved" | "denied" | "expired";
  argumentsSummary: JsonRecord;
  resultSummary: string;
  contentPreview: string;
  error?: AppError;
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
};

export function buildToolCallViews(events: RuntimeEvent[]): ToolCallView[] {
  const calls = new Map<string, ToolCallView>();

  for (const event of events) {
    if (!event.type.startsWith("tool.call.")) continue;
    const payload = asRecord(event.payload);
    const toolCall = asRecord(payload.tool_call);
    const id = typeof toolCall.id === "string" ? toolCall.id : event.id;
    const existing = calls.get(id);
    const status = event.type === "tool.call.failed"
      ? "failed"
      : event.type === "tool.call.finished"
        ? "completed"
        : "running";

    const startedAt = existing?.startedAt ?? (
      event.type === "tool.call.started" ? event.timestamp : undefined
    );
    const completedAt = event.type === "tool.call.started"
      ? existing?.completedAt
      : event.timestamp;
    const explicitDuration = numeric(toolCall.duration_ms) ?? numeric(payload.duration_ms);

    calls.set(id, {
      id,
      toolName: stringValue(toolCall.tool_name, existing?.toolName ?? "未知工具"),
      provider: stringValue(toolCall.provider, existing?.provider ?? "native"),
      riskLevel: stringValue(toolCall.risk_level, existing?.riskLevel ?? "L0") as RiskLevel,
      status,
      permissionStatus: permissionStatus(
        toolCall.permission_status,
        existing?.permissionStatus ?? "not_required"
      ),
      argumentsSummary: Object.keys(asRecord(toolCall.arguments_summary)).length
        ? asRecord(toolCall.arguments_summary)
        : existing?.argumentsSummary ?? {},
      resultSummary: stringValue(
        asRecord(toolCall.result).summary,
        existing?.resultSummary ?? ""
      ),
      contentPreview: stringValue(
        asRecord(payload.content_summary).preview,
        existing?.contentPreview ?? ""
      ),
      error: asAppError(toolCall.error) ?? existing?.error,
      startedAt,
      completedAt,
      durationMs: explicitDuration ?? durationBetween(startedAt, completedAt),
    });
  }

  return [...calls.values()].sort((a, b) =>
    (a.startedAt ?? "").localeCompare(b.startedAt ?? "")
  );
}

function permissionStatus(
  value: unknown,
  fallback: ToolCallView["permissionStatus"]
): ToolCallView["permissionStatus"] {
  return value === "pending" || value === "approved" || value === "denied"
    || value === "expired" || value === "not_required"
    ? value
    : fallback;
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value ? value : fallback;
}

function numeric(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function durationBetween(start?: string, end?: string): number | undefined {
  if (!start || !end) return undefined;
  const duration = new Date(end).getTime() - new Date(start).getTime();
  return Number.isFinite(duration) ? Math.max(0, duration) : undefined;
}

function asAppError(value: unknown): AppError | undefined {
  const error = asRecord(value);
  if (typeof error.code !== "string" || typeof error.message !== "string") return undefined;
  return {
    code: error.code,
    message: error.message,
    category: typeof error.category === "string" ? error.category as AppError["category"] : "tool",
    recoverable: error.recoverable === true,
  };
}
