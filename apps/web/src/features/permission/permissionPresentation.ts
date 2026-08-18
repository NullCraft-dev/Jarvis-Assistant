import type {
  PermissionDecisionType,
  PermissionRequestDTO,
  PermissionScopeDTO,
  RiskLevel,
  RuntimeEvent,
} from "@jarvis/shared";

export type PermissionDecisionPresentation = {
  label: string;
  description: string;
  persistent: boolean;
  tone: "approve" | "caution" | "deny";
};

export type PermissionScopePresentation = {
  label: string;
  description: string;
  facts: Array<{ label: string; value: string }>;
};

export type PermissionArgumentRow = {
  key: string;
  label: string;
  value: string;
  monospace: boolean;
};

const DECISION_PRESENTATIONS: Record<PermissionDecisionType, PermissionDecisionPresentation> = {
  allow_once: {
    label: "允许本次操作",
    description: "仅对当前这一次请求有效，不会创建后续授权规则。",
    persistent: false,
    tone: "approve",
  },
  allow_for_task: {
    label: "本任务内允许",
    description: "在当前任务结束前，对服务端声明的相同能力范围有效。",
    persistent: true,
    tone: "caution",
  },
  always_allow_for_tool_and_path: {
    label: "持续允许此工具和路径",
    description: "会创建持久授权，只覆盖当前工具与指定路径组合。",
    persistent: true,
    tone: "caution",
  },
  always_allow_for_workspace: {
    label: "持续允许当前工作区",
    description: "会创建工作区级持久授权，影响后续符合该范围的请求。",
    persistent: true,
    tone: "caution",
  },
  deny: {
    label: "拒绝操作",
    description: "本次操作不会执行；拒绝决定仍会写入审计记录。",
    persistent: false,
    tone: "deny",
  },
};

const ARGUMENT_LABELS: Record<string, string> = {
  path: "目标路径",
  source_path: "来源路径",
  destination_path: "目标路径",
  filename: "文件名",
  workspace_root: "工作区",
  workspace_id: "工作区 ID",
  artifact_id: "Artifact ID",
  document_id: "文档 ID",
  expected_version: "预期版本",
  enabled: "启用状态",
  size_bytes: "内容大小",
  sha256: "内容指纹",
  query: "查询",
  server_id: "MCP Server",
};

const SENSITIVE_KEY = /(authorization|cookie|credential|password|passwd|secret|token|api[_-]?key|private[_-]?key)/i;
const MAX_ARGUMENT_ROWS = 16;
const MAX_VALUE_LENGTH = 240;

export function getPermissionDecisionPresentation(
  decision: PermissionDecisionType,
): PermissionDecisionPresentation {
  return DECISION_PRESENTATIONS[decision];
}

export function getPermissionScopePresentation(
  scope: PermissionScopeDTO,
): PermissionScopePresentation {
  const facts: Array<{ label: string; value: string }> = [];
  if (scope.workspace_path) facts.push({ label: "工作区", value: scope.workspace_path });
  if (scope.path) facts.push({ label: "路径", value: scope.path });
  if (scope.tool_name) facts.push({ label: "工具", value: scope.tool_name });
  if (scope.mcp_server_id) facts.push({ label: "MCP Server", value: scope.mcp_server_id });
  if (scope.task_id) facts.push({ label: "任务", value: scope.task_id });

  switch (scope.type) {
    case "once":
      return {
        label: "仅当前操作",
        description: "批准只对这一次权限请求有效，不会自动批准后续操作。",
        facts,
      };
    case "task":
      return {
        label: "当前任务",
        description: "授权最长持续到当前任务结束，不影响其他任务。",
        facts,
      };
    case "tool_path":
      return {
        label: "指定工具与路径",
        description: "授权只覆盖列出的工具和路径组合。",
        facts,
      };
    case "workspace":
      return {
        label: "当前工作区",
        description: "授权覆盖当前工作区内服务端允许的对应操作。",
        facts,
      };
    case "global":
      return {
        label: "全局范围",
        description: "授权可能影响后续任务；仅在服务端明确允许时提供。",
        facts,
      };
  }
}

export function getRiskImpact(level: RiskLevel): string {
  const impacts: Record<RiskLevel, string> = {
    L0: "只读操作，不修改本地或外部状态。",
    L1: "低风险操作，会产生可审计的有限状态变化。",
    L2: "将在受控范围内写入或创建内容。",
    L3: "会产生需要你明确确认的外部或本地影响。",
    L4: "高风险受限操作，每次都必须单独确认，不能永久批准。",
    L5: "禁止操作，不能通过界面批准。",
  };
  return impacts[level];
}

export function formatPermissionArguments(
  summary: Record<string, unknown>,
): PermissionArgumentRow[] {
  const entries = Object.entries(summary).slice(0, MAX_ARGUMENT_ROWS);
  return entries.map(([key, value]) => ({
    key,
    label: ARGUMENT_LABELS[key] ?? key,
    value: formatArgumentValue(key, value),
    monospace: isMonospaceArgument(key, value),
  }));
}

function formatArgumentValue(key: string, value: unknown): string {
  if (SENSITIVE_KEY.test(key)) return "已隐藏敏感值";
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    if (record.redacted === true) {
      const parts = ["内容已脱敏"];
      if (typeof record.size_bytes === "number") parts.push(formatBytes(record.size_bytes));
      if (typeof record.sha256 === "string" && record.sha256) {
        parts.push(`SHA-256 ${shortFingerprint(record.sha256)}`);
      }
      return parts.join(" · ");
    }
  }
  if (typeof value === "boolean") return value ? "是" : "否";
  if (value === null || value === undefined) return "—";
  if (typeof value === "number" && key.endsWith("_bytes")) return formatBytes(value);

  const rendered = typeof value === "string"
    ? value
    : safeStringify(value);
  return rendered.length > MAX_VALUE_LENGTH
    ? `${rendered.slice(0, MAX_VALUE_LENGTH)}…`
    : rendered;
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return "无法显示的结构化摘要";
  }
}

function isMonospaceArgument(key: string, value: unknown): boolean {
  return typeof value === "string" && (
    key.includes("path") ||
    key.endsWith("_id") ||
    key === "sha256" ||
    key === "workspace_root"
  );
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return String(value);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

function shortFingerprint(value: string): string {
  return value.length > 16 ? `${value.slice(0, 12)}…${value.slice(-4)}` : value;
}

export function getPermissionEventPresentation(event: RuntimeEvent): {
  label: string;
  summary: string;
  riskLevel?: RiskLevel;
} | null {
  const payload = event.payload as Record<string, unknown>;
  if (event.type === "permission.required") {
    const request = payload.request as PermissionRequestDTO | undefined;
    return {
      label: "等待用户确认",
      summary: request?.action_summary ?? "需要确认一项操作",
      riskLevel: request?.risk_level,
    };
  }
  if (event.type === "permission.resolved") {
    const decision = payload.decision;
    if (typeof decision === "string" && decision in DECISION_PRESENTATIONS) {
      const presentation = getPermissionDecisionPresentation(
        decision as PermissionDecisionType,
      );
      return {
        label: presentation.label,
        summary: decision === "deny"
          ? "拒绝决定已记录，操作不会执行。"
          : "授权决定已受理，工具结果仍以后续运行事件为准。",
      };
    }
    return { label: "权限已处理", summary: "决定已记录。" };
  }
  if (event.type === "permission.expired") {
    return {
      label: "权限请求已失效",
      summary: "该请求已过期或因运行结束而关闭，不能再提交决定。",
    };
  }
  return null;
}
