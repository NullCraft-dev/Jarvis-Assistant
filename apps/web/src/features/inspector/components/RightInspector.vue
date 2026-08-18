<script setup lang="ts">
// 右侧检查器面板：Context / Tools / Permissions / Logs
// 真源：docs/11-frontend-app-ui-design.md § Right Inspector

import { useUiStore, type InspectorTab } from "@/stores/uiStore";
import { useTaskStore } from "@/stores/taskStore";
import { useRunStore } from "@/stores/runStore";
import { usePermissionStore } from "@/stores/permissionStore";
import { computed } from "vue";
import type { ModelContextPreparedPayload, RuntimeEvent } from "@jarvis/shared";
import {
  X,
  FolderOpen,
  Wrench,
  Shield,
  ScrollText,
} from "@lucide/vue";
import RiskBadge from "@/components/ui/RiskBadge.vue";
import ToolCallDetails from "./ToolCallDetails.vue";
import { buildToolCallViews } from "@/features/inspector/composables/toolCallView";
import { getLatestRunControlView } from "@/features/timeline/runControlPresentation";
import {
  getPermissionEventPresentation,
  getPermissionScopePresentation,
} from "@/features/permission/permissionPresentation";
import { getRuntimeEventPresentation } from "@/features/timeline/runtimeEventPresentation";

const props = withDefaults(defineProps<{ drawer?: boolean }>(), {
  drawer: false,
});

const ui = useUiStore();
const taskStore = useTaskStore();
const runStore = useRunStore();
const permissionStore = usePermissionStore();

const activeTask = computed(() => taskStore.activeTask);
const activeRunId = computed(() => taskStore.activeRunId);
const events = computed(() =>
  activeRunId.value ? runStore.getEvents(activeRunId.value) : []
);

const toolCalls = computed(() => buildToolCallViews(events.value));
const latestRunControl = computed(() => getLatestRunControlView(events.value));
const contextStats = computed(() => {
  const event = [...events.value]
    .reverse()
    .find((item) => item.type === "model.context.prepared");
  return event?.payload as ModelContextPreparedPayload | undefined;
});
const activeSkill = computed(() => {
  const stats = contextStats.value;
  if (
    !stats ||
    typeof stats.skill_id !== "string" ||
    !stats.skill_id ||
    typeof stats.skill_version !== "string" ||
    !stats.skill_version ||
    typeof stats.skill_fingerprint !== "string" ||
    !stats.skill_fingerprint
  ) {
    return undefined;
  }
  return {
    id: stats.skill_id,
    version: stats.skill_version,
    fingerprint: stats.skill_fingerprint,
  };
});
const contextUsagePercent = computed(() => {
  if (!contextStats.value || contextStats.value.input_budget_tokens <= 0) return 0;
  return Math.min(
    100,
    Math.round(
      contextStats.value.estimated_input_tokens /
        contextStats.value.input_budget_tokens *
        100
    )
  );
});

const pendingPermissions = computed(() =>
  activeRunId.value ? permissionStore.getPendingByRun(activeRunId.value) : []
);

function permissionRequestFromEvent(event: RuntimeEvent) {
  const payload = event.payload as { request?: unknown };
  const request = payload.request;
  if (!request || typeof request !== "object") return null;
  return request as {
    id?: string;
    risk_level?: "L0" | "L1" | "L2" | "L3" | "L4" | "L5";
  };
}

function permissionRequestId(event: RuntimeEvent) {
  const payload = event.payload as { request_id?: unknown };
  const request = permissionRequestFromEvent(event);
  if (typeof request?.id === "string") return request.id;
  return typeof payload.request_id === "string" ? payload.request_id : null;
}

const permissionEventViews = computed(() => {
  const pendingIds = new Set(pendingPermissions.value.map((request) => request.id));
  const byRequest = new Map<string, {
    event: RuntimeEvent;
    presentation: NonNullable<ReturnType<typeof getPermissionEventPresentation>>;
    riskLevel?: "L0" | "L1" | "L2" | "L3" | "L4" | "L5";
  }>();

  for (const event of events.value) {
    if (!["permission.required", "permission.resolved", "permission.expired"].includes(event.type)) {
      continue;
    }
    const requestId = permissionRequestId(event);
    const presentation = getPermissionEventPresentation(event);
    if (!requestId || !presentation) continue;
    const existing = byRequest.get(requestId);
    byRequest.set(requestId, {
      event,
      presentation,
      riskLevel: permissionRequestFromEvent(event)?.risk_level ?? existing?.riskLevel,
    });
  }

  return [...byRequest.entries()]
    .filter(([requestId]) => !pendingIds.has(requestId))
    .map(([, view]) => view)
    .reverse();
});

const technicalEvents = computed(() =>
  [...events.value].reverse().map((event) => ({
    event,
    presentation: getRuntimeEventPresentation(event),
  }))
);

const tabs = computed<{ key: InspectorTab; label: string; icon: typeof FolderOpen; count?: number }[]>(() => [
  { key: "context", label: "上下文", icon: FolderOpen },
  { key: "tools", label: "工具", icon: Wrench, count: toolCalls.value.length },
  {
    key: "permissions",
    label: "权限",
    icon: Shield,
    count: permissionEventViews.value.length + pendingPermissions.value.length,
  },
  { key: "logs", label: "技术", icon: ScrollText, count: events.value.length },
]);
</script>

<template>
  <aside
    class="flex h-full shrink-0 flex-col border-l border-[var(--color-border)] bg-[var(--color-surface)]"
    :class="props.drawer ? 'w-[min(360px,calc(100vw-2rem))]' : 'w-[320px]'"
    :aria-label="props.drawer ? '检查器抽屉' : '检查器'"
  >
    <!-- 头部 -->
    <div class="flex items-center justify-between px-3 py-2 border-b border-[var(--color-border)]">
      <span class="text-sm font-medium text-[var(--color-text)]">检查器</span>
      <button
        class="text-[var(--color-muted)] hover:text-[var(--color-text)]"
        @click="ui.toggleInspector()"
      >
        <X :size="16" />
      </button>
    </div>

    <!-- Tabs -->
    <div class="flex border-b border-[var(--color-border)]">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="flex-1 flex items-center justify-center gap-1 py-2 text-sm transition-colors"
        :class="
          ui.inspectorTab === tab.key
            ? 'text-[var(--color-accent)] border-b-2 border-[var(--color-accent)]'
            : 'text-[var(--color-muted)] hover:text-[var(--color-text)]'
        "
        @click="ui.setInspectorTab(tab.key)"
      >
        <component :is="tab.icon" :size="13" />
        {{ tab.label }}
        <span
          v-if="tab.count"
          class="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] tabular-nums text-gray-500"
        >
          {{ tab.count }}
        </span>
      </button>
    </div>

    <!-- 内容 -->
    <div class="flex-1 overflow-y-auto p-3">
      <!-- Context Tab -->
      <div v-if="ui.inspectorTab === 'context'" class="space-y-3 text-sm">
        <div v-if="activeTask" class="text-xs font-medium uppercase tracking-wide text-[var(--color-muted)]">
          运行约束
        </div>
        <div v-if="activeTask?.workspace_path">
          <div class="text-xs text-[var(--color-muted)] mb-1">工作区边界</div>
          <div class="text-xs font-mono">{{ activeTask.workspace_path }}</div>
        </div>
        <div
          v-if="latestRunControl"
          class="rounded border border-blue-100 bg-blue-50 p-2.5 text-xs"
          data-testid="run-control-view"
        >
          <div class="font-medium text-blue-900">{{ latestRunControl.title }}</div>
          <div class="mt-1 text-blue-800">{{ latestRunControl.summary }}</div>
          <div class="mt-1 text-[11px] text-blue-700">
            执行位置：{{ latestRunControl.checkpointLabel }}
          </div>
        </div>
        <div
          v-if="contextStats"
          class="rounded border border-[var(--color-border)] bg-gray-50 p-2.5 space-y-2"
        >
          <div class="flex items-center justify-between">
            <div class="text-xs text-[var(--color-muted)]">模型上下文</div>
            <div class="text-xs font-medium">{{ contextUsagePercent }}%</div>
          </div>
          <div class="h-1.5 overflow-hidden rounded bg-gray-200">
            <div
              class="h-full rounded bg-[var(--color-accent)] transition-all"
              :style="{ width: `${contextUsagePercent}%` }"
            />
          </div>
          <div class="text-xs">
            {{ contextStats.estimated_input_tokens.toLocaleString() }} /
            {{ contextStats.input_budget_tokens.toLocaleString() }} tokens
          </div>
          <div class="grid grid-cols-3 gap-2 text-xs">
            <div>
              <div class="text-[var(--color-muted)]">历史轮次</div>
              <div>
                {{ contextStats.included_history_turns }} 保留
                <span v-if="contextStats.dropped_history_turns">
                  · {{ contextStats.dropped_history_turns }} 裁剪
                </span>
              </div>
            </div>
            <div>
              <div class="text-[var(--color-muted)]">工具观测</div>
              <div>
                {{ contextStats.included_observations }} 保留
                <span v-if="contextStats.dropped_observations">
                  · {{ contextStats.dropped_observations }} 裁剪
                </span>
              </div>
            </div>
            <div>
              <div class="text-[var(--color-muted)]">长期记忆</div>
              <div>
                {{ contextStats.included_memories }} 保留
                <span v-if="contextStats.dropped_memories">
                  · {{ contextStats.dropped_memories }} 裁剪
                </span>
              </div>
            </div>
          </div>
          <div class="text-[11px] text-[var(--color-muted)]">
            {{ contextStats.provider }} / {{ contextStats.model_name }}
            · {{ contextStats.policy_version }}
          </div>
          <div
            v-if="activeSkill"
            class="border-t border-[var(--color-border)] pt-2 text-xs"
          >
            <div class="text-[var(--color-muted)]">已加载 Skill</div>
            <div class="mt-0.5 font-medium">
              {{ activeSkill.id }} · v{{ activeSkill.version }}
            </div>
            <div
              class="mt-1 break-all font-mono text-[10px] text-[var(--color-muted)]"
              :title="activeSkill.fingerprint"
            >
              {{ activeSkill.fingerprint }}
            </div>
          </div>
        </div>
        <div
          v-else-if="activeTask"
          class="text-xs text-[var(--color-muted)]"
        >
          模型调用后会显示上下文预算
        </div>
        <div v-if="!activeTask" class="text-xs text-[var(--color-muted)]">
          暂无活跃任务
        </div>
      </div>

      <!-- Tools Tab -->
      <div v-if="ui.inspectorTab === 'tools'" class="space-y-2">
        <ToolCallDetails
          v-for="call in toolCalls"
          :key="call.id"
          :call="call"
        />
        <div v-if="toolCalls.length === 0" class="text-xs text-[var(--color-muted)]">
          暂无工具调用
        </div>
      </div>

      <!-- Permissions Tab -->
      <div v-if="ui.inspectorTab === 'permissions'" class="space-y-2">
        <div
          v-for="item in permissionEventViews"
          :key="item.event.id"
          class="rounded border border-[var(--color-border)] bg-gray-50 p-2.5 text-sm"
        >
          <div class="flex items-center justify-between gap-2">
            <span class="font-medium">{{ item.presentation.label }}</span>
            <RiskBadge v-if="item.riskLevel" :level="item.riskLevel" />
          </div>
          <div class="mt-1 text-xs text-[var(--color-muted)]">{{ item.presentation.summary }}</div>
          <div class="mt-1 text-[11px] text-[var(--color-muted)]">
            {{ new Date(item.event.timestamp).toLocaleTimeString() }}
          </div>
        </div>
        <div
          v-for="req in pendingPermissions"
          :key="'p'+req.id"
          class="rounded border border-amber-200 bg-amber-50 p-2.5 text-sm"
        >
          <div class="flex items-center justify-between gap-2">
            <div class="min-w-0 break-all font-mono text-xs font-medium text-amber-900">{{ req.tool_name }}</div>
            <RiskBadge :level="req.risk_level" />
          </div>
          <div class="mt-1 text-xs text-amber-800">{{ req.action_summary }}</div>
          <div class="mt-2 rounded bg-white/70 p-2 text-xs text-[var(--color-muted)]">
            <span class="font-medium text-[var(--color-text)]">
              {{ getPermissionScopePresentation(req.scope).label }}
            </span>
            · {{ getPermissionScopePresentation(req.scope).description }}
          </div>
        </div>
        <div
          v-if="permissionEventViews.length === 0 && pendingPermissions.length === 0"
          class="text-xs text-[var(--color-muted)]"
        >
          暂无权限事件
        </div>
      </div>

      <!-- Logs Tab -->
      <div v-if="ui.inspectorTab === 'logs'" class="space-y-2">
        <div class="rounded border border-blue-100 bg-blue-50 p-2.5 text-xs text-blue-800">
          技术诊断层用于追溯内部事件和关联 ID；日常进度请查看对话中的“执行过程”。
        </div>
        <details
          v-for="item in technicalEvents"
          :key="item.event.id"
          class="rounded border border-[var(--color-border)] bg-gray-50 px-2.5 py-2 text-xs"
        >
          <summary class="cursor-pointer list-none">
            <div class="flex items-start justify-between gap-2">
              <span class="font-medium text-[var(--color-text)]">{{ item.presentation.title }}</span>
              <span class="shrink-0 text-[11px] text-[var(--color-muted)]">
                {{ new Date(item.event.timestamp).toLocaleTimeString() }}
              </span>
            </div>
            <p v-if="item.presentation.summary" class="mt-1 text-[11px] text-[var(--color-muted)]">
              {{ item.presentation.summary }}
            </p>
          </summary>
          <dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 border-t border-gray-200 pt-2 text-[11px]">
            <dt class="text-[var(--color-muted)]">内部事件</dt>
            <dd class="break-all font-mono">{{ item.event.type }}</dd>
            <dt class="text-[var(--color-muted)]">事件 ID</dt>
            <dd class="break-all font-mono">{{ item.event.id }}</dd>
            <template v-if="item.event.step_id">
              <dt class="text-[var(--color-muted)]">步骤 ID</dt>
              <dd class="break-all font-mono">{{ item.event.step_id }}</dd>
            </template>
          </dl>
        </details>
        <div v-if="technicalEvents.length === 0" class="text-xs text-[var(--color-muted)]">
          暂无事件
        </div>
      </div>
    </div>
  </aside>
</template>
