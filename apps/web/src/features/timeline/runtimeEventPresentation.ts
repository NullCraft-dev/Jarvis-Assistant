import type { RuntimeEvent, RuntimeEventType } from "@jarvis/shared";
import { getPermissionEventPresentation } from "@/features/permission/permissionPresentation";
import { getRunControlView } from "@/features/timeline/runControlPresentation";

type JsonRecord = Record<string, unknown>;

export type TimelineCategory =
  | "run"
  | "model"
  | "tool"
  | "permission"
  | "artifact"
  | "step"
  | "technical";

export type TimelineTone = "neutral" | "active" | "success" | "warning" | "error";
export type TimelineDetailTarget = "context" | "tools" | "permissions" | "logs";

export type RuntimeEventPresentation = {
  title: string;
  summary: string;
  category: TimelineCategory;
  categoryLabel: string;
  tone: TimelineTone;
  detailTarget?: TimelineDetailTarget;
};

const CATEGORY_LABELS: Record<TimelineCategory, string> = {
  run: "运行",
  model: "模型",
  tool: "工具",
  permission: "权限",
  artifact: "交付物",
  step: "步骤",
  technical: "技术",
};

const TIMELINE_HIDDEN_TYPES = new Set<RuntimeEventType>([
  "task.created",
  "task.updated",
  "agent.step.started",
  "agent.step.updated",
  "agent.step.completed",
  "model.context.prepared",
  "model.delta",
  "log.appended",
]);

const FAILURE_TYPES = new Set<RuntimeEventType>([
  "agent.run.failed",
  "agent.step.failed",
  "model.call.failed",
  "tool.call.failed",
  "mcp.call.failed",
]);

const SUCCESS_TYPES = new Set<RuntimeEventType>([
  "agent.run.completed",
  "model.call.completed",
  "tool.call.finished",
  "mcp.call.finished",
  "artifact.created",
]);

const ACTIVE_TYPES = new Set<RuntimeEventType>([
  "agent.run.started",
  "agent.run.resumed",
  "model.call.started",
  "tool.call.started",
  "mcp.call.started",
]);

export function getRuntimeEventPresentation(event: RuntimeEvent): RuntimeEventPresentation {
  const payload = asRecord(event.payload);
  const category = categoryFor(event.type);
  const permission = getPermissionEventPresentation(event);
  const runControl = getRunControlView(event);

  if (permission) {
    return {
      title: permission.label,
      summary: permission.summary,
      category,
      categoryLabel: CATEGORY_LABELS[category],
      tone: event.type === "permission.expired"
        ? "warning"
        : event.type === "permission.resolved"
          ? "success"
          : "warning",
      detailTarget: "permissions",
    };
  }

  if (runControl) {
    return {
      title: runControl.title,
      summary: runControl.summary,
      category,
      categoryLabel: CATEGORY_LABELS[category],
      tone: toneFor(event.type),
    };
  }

  return {
    title: titleFor(event.type, payload),
    summary: summaryFor(event, payload),
    category,
    categoryLabel: CATEGORY_LABELS[category],
    tone: toneFor(event.type),
    detailTarget: detailTargetFor(event.type),
  };
}

/**
 * Timeline 只表达用户需要理解的过程节点：
 * - 流式正文、上下文预算和原始日志由 Conversation / Inspector 各自承载；
 * - 已有终态时隐藏同一次调用的 started 事件；
 * - final_response artifact 已由对话正文表达，不在 Timeline 重复。
 */
export function buildTimelineEvents(events: RuntimeEvent[]): RuntimeEvent[] {
  const completedLifecycleKeys = new Set(
    events
      .filter(isLifecycleTerminal)
      .map(lifecycleKey)
      .filter((key): key is string => Boolean(key))
  );
  const settledPermissionIds = new Set(
    events
      .filter((event) => event.type === "permission.resolved" || event.type === "permission.expired")
      .map(permissionRequestId)
      .filter((requestId): requestId is string => Boolean(requestId))
  );

  return events.filter((event) => {
    if (TIMELINE_HIDDEN_TYPES.has(event.type)) return false;
    if (isFinalResponseArtifact(event)) return false;
    if (
      event.type === "permission.required" &&
      settledPermissionIds.has(permissionRequestId(event) ?? "")
    ) {
      return false;
    }
    if (isLifecycleStart(event)) {
      const key = lifecycleKey(event);
      if (key && completedLifecycleKeys.has(key)) return false;
    }
    return true;
  });
}

export function summarizeTimeline(events: RuntimeEvent[]): string {
  const visible = buildTimelineEvents(events);
  const toolCount = visible.filter((event) =>
    ["tool.call.started", "tool.call.finished", "tool.call.failed", "mcp.call.started", "mcp.call.finished", "mcp.call.failed"]
      .includes(event.type)
  ).length;
  const permissionCount = visible.filter((event) =>
    ["permission.required", "permission.resolved", "permission.expired"].includes(event.type)
  ).length;
  const failureCount = visible.filter((event) => FAILURE_TYPES.has(event.type)).length;

  const parts = [`${visible.length} 个关键节点`];
  if (toolCount) parts.push(`${toolCount} 个工具节点`);
  if (permissionCount) parts.push(`${permissionCount} 个权限节点`);
  if (failureCount) parts.push(`${failureCount} 个失败节点`);
  return parts.join(" · ");
}

function titleFor(type: RuntimeEventType, payload: JsonRecord): string {
  switch (type) {
    case "task.created":
      return "任务已创建";
    case "task.updated":
      return "任务已更新";
    case "agent.run.started":
      return "开始执行任务";
    case "agent.run.paused":
      return "运行已暂停";
    case "agent.run.resumed":
      return "运行已恢复";
    case "agent.run.completed":
      return "任务执行完成";
    case "agent.run.failed":
      return "任务执行失败";
    case "agent.run.cancelled":
      return "运行已取消";
    case "agent.step.started":
      return stringValue(asRecord(payload.step).title, "步骤开始");
    case "agent.step.updated":
      return stringValue(asRecord(payload.step).title, "步骤已更新");
    case "agent.step.completed":
      return stringValue(asRecord(payload.step).title, "步骤完成");
    case "agent.step.failed":
      return stringValue(asRecord(payload.step).title, "步骤失败");
    case "model.call.started":
      return "模型正在处理";
    case "model.context.prepared":
      return "模型上下文已准备";
    case "model.delta":
      return "正在生成回复";
    case "model.call.completed":
      return modelActionLabel(payload);
    case "model.call.failed":
      return "模型处理失败";
    case "tool.call.started":
      return `正在执行 ${toolName(payload)}`;
    case "tool.call.finished":
      return `${toolName(payload)} 已完成`;
    case "tool.call.failed":
      return `${toolName(payload)} 执行失败`;
    case "mcp.call.started":
      return "外部工具正在执行";
    case "mcp.call.finished":
      return "外部工具执行完成";
    case "mcp.call.failed":
      return "外部工具执行失败";
    case "artifact.created":
      return artifactTitle(payload);
    case "log.appended":
      return "运行诊断已更新";
    case "permission.required":
    case "permission.resolved":
    case "permission.expired":
      return "权限状态已更新";
  }
}

function summaryFor(event: RuntimeEvent, payload: JsonRecord): string {
  switch (event.type) {
    case "task.created":
      return "目标已经进入执行队列。";
    case "task.updated":
      return "任务状态已由后端更新。";
    case "agent.run.started":
      return "Agent 已接手任务并开始规划执行。";
    case "agent.run.paused":
      return "已在安全边界暂停，可以稍后继续。";
    case "agent.run.resumed":
      return "已从持久化状态继续执行。";
    case "agent.run.completed": {
      const steps = positiveNumber(payload.total_steps);
      return steps
        ? `结果已验证并保存，共完成 ${steps} 个步骤。`
        : "结果已验证并保存。";
    }
    case "agent.run.failed":
      return safeErrorSummary(payload, "运行未能完成，请在状态区查看恢复方式。");
    case "agent.run.cancelled":
      return "运行已停止，已完成的过程证据仍会保留。";
    case "agent.step.started":
    case "agent.step.updated":
    case "agent.step.completed":
      return stringValue(asRecord(payload.step).summary, "");
    case "agent.step.failed":
      return safeErrorSummary(asRecord(payload.step), "该步骤未能完成。");
    case "model.call.started":
      return modelIdentity(payload);
    case "model.context.prepared": {
      const used = positiveNumber(payload.estimated_input_tokens);
      const budget = positiveNumber(payload.input_budget_tokens);
      return used && budget ? `已使用 ${used.toLocaleString()} / ${budget.toLocaleString()} tokens。` : "";
    }
    case "model.delta": {
      const delta = stringValue(payload.delta, "");
      return delta ? `回复已新增 ${Array.from(delta).length} 个字符。` : "回复生成中。";
    }
    case "model.call.completed": {
      const duration = durationLabel(payload.duration_ms);
      return duration ? `模型处理完成，用时 ${duration}。` : "模型处理完成。";
    }
    case "model.call.failed": {
      const error = safeErrorSummary(payload, "模型暂时未能完成处理。");
      return payload.recoverable === true ? `${error} 可以从安全检查点重试。` : error;
    }
    case "tool.call.started":
      return toolScopeSummary(payload) || "工具已进入受控执行链路。";
    case "tool.call.finished": {
      const summary = stringValue(asRecord(asRecord(payload.tool_call).result).summary, "");
      const duration = durationLabel(asRecord(payload.tool_call).duration_ms ?? payload.duration_ms);
      if (summary && duration) return `${summary} · ${duration}`;
      return summary || (duration ? `工具执行完成，用时 ${duration}。` : "工具执行完成。");
    }
    case "tool.call.failed":
      return safeErrorSummary(asRecord(payload.tool_call), "工具未能完成执行。");
    case "mcp.call.started":
      return "调用已通过 ToolGateway 进入外部能力适配器。";
    case "mcp.call.finished":
      return "外部能力已返回结果。";
    case "mcp.call.failed":
      return safeErrorSummary(payload, "外部能力调用失败。");
    case "artifact.created":
      return artifactSummary(payload);
    case "log.appended":
      return "详细内容仅在技术诊断层查看。";
    case "permission.required":
    case "permission.resolved":
    case "permission.expired":
      return "";
  }
}

function categoryFor(type: RuntimeEventType): TimelineCategory {
  if (type.startsWith("agent.run.")) return "run";
  if (type.startsWith("agent.step.")) return "step";
  if (type.startsWith("model.")) return "model";
  if (type.startsWith("tool.") || type.startsWith("mcp.")) return "tool";
  if (type.startsWith("permission.")) return "permission";
  if (type === "artifact.created") return "artifact";
  return "technical";
}

function toneFor(type: RuntimeEventType): TimelineTone {
  if (FAILURE_TYPES.has(type)) return "error";
  if (SUCCESS_TYPES.has(type)) return "success";
  if (ACTIVE_TYPES.has(type)) return "active";
  if (type === "agent.run.cancelled" || type === "agent.run.paused") return "warning";
  return "neutral";
}

function detailTargetFor(type: RuntimeEventType): TimelineDetailTarget | undefined {
  if (type.startsWith("tool.") || type.startsWith("mcp.")) return "tools";
  if (type.startsWith("permission.")) return "permissions";
  if (
    type === "model.call.failed" ||
    type === "agent.run.failed" ||
    type === "agent.step.failed" ||
    type === "log.appended"
  ) {
    return "logs";
  }
  if (type === "model.context.prepared") return "context";
  return undefined;
}

function modelActionLabel(payload: JsonRecord): string {
  switch (stringValue(payload.action_type, "")) {
    case "intent_extraction":
      return "任务理解完成";
    case "tool_call":
      return "执行方案已确定";
    case "final_response":
      return "最终回复已生成";
    default:
      return "模型处理完成";
  }
}

function modelIdentity(payload: JsonRecord): string {
  const provider = stringValue(payload.provider, "");
  const model = stringValue(payload.model_name, "");
  return [provider, model].filter(Boolean).join(" · ");
}

function toolName(payload: JsonRecord): string {
  return stringValue(asRecord(payload.tool_call).tool_name, "工具");
}

function toolScopeSummary(payload: JsonRecord): string {
  const toolCall = asRecord(payload.tool_call);
  const argumentsSummary = asRecord(toolCall.arguments_summary);
  const path = stringValue(argumentsSummary.path, "");
  const workspace = stringValue(argumentsSummary.workspace_root, "");
  if (path) return `目标路径：${path}`;
  if (workspace) return `工作区：${workspace}`;
  return stringValue(toolCall.action_summary, "");
}

function safeErrorSummary(payload: JsonRecord, fallback: string): string {
  const error = asRecord(payload.error);
  const message = stringValue(error.message, "");
  const code = stringValue(error.code, stringValue(payload.error_code, ""));
  if (message && code) return `${message}（${code}）`;
  return message || (code ? `错误码 ${code}` : fallback);
}

function artifactTitle(payload: JsonRecord): string {
  const artifact = asRecord(payload.artifact);
  return stringValue(artifact.title, "交付物已保存");
}

function artifactSummary(payload: JsonRecord): string {
  const artifact = asRecord(payload.artifact);
  const kind = stringValue(artifact.kind, "");
  return kind ? `${kind} 交付物已写入持久化存储。` : "交付物已写入持久化存储。";
}

function isFinalResponseArtifact(event: RuntimeEvent): boolean {
  if (event.type !== "artifact.created") return false;
  return stringValue(asRecord(asRecord(event.payload).artifact).purpose, "") === "final_response";
}

function isLifecycleStart(event: RuntimeEvent): boolean {
  return ["model.call.started", "tool.call.started", "mcp.call.started"].includes(event.type);
}

function isLifecycleTerminal(event: RuntimeEvent): boolean {
  return [
    "model.call.completed",
    "model.call.failed",
    "tool.call.finished",
    "tool.call.failed",
    "mcp.call.finished",
    "mcp.call.failed",
  ].includes(event.type);
}

function lifecycleKey(event: RuntimeEvent): string | null {
  const payload = asRecord(event.payload);
  if (event.type.startsWith("model.call.")) {
    const callId = stringValue(payload.call_id, stringValue(payload.model_call_id, ""));
    const identity = callId || event.step_id || "";
    return identity ? `model:${identity}` : null;
  }
  if (event.type.startsWith("tool.call.")) {
    const callId = stringValue(asRecord(payload.tool_call).id, "");
    return callId ? `tool:${callId}` : null;
  }
  if (event.type.startsWith("mcp.call.")) {
    const callId = stringValue(payload.call_id, stringValue(payload.id, ""));
    return callId ? `mcp:${callId}` : null;
  }
  return null;
}

function permissionRequestId(event: RuntimeEvent): string | null {
  const payload = asRecord(event.payload);
  const request = asRecord(payload.request);
  return stringValue(request.id, stringValue(payload.request_id, "")) || null;
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value ? value : fallback;
}

function positiveNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

function durationLabel(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "";
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`;
}
