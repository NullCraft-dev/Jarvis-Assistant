<script setup lang="ts">
// 顶部状态栏：工作区、模型策略、运行状态、Worker 状态（3B）
// 真源：docs/11-frontend-app-ui-design.md § Header

import { useTaskStore } from "@/stores/taskStore";
import { useRunStore } from "@/stores/runStore";
import { useUiStore } from "@/stores/uiStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { computed, onMounted, onUnmounted, ref } from "vue";
import { Activity, Wifi, WifiOff, Zap, Cpu, FolderPlus } from "@lucide/vue";
import { getWorkers } from "@/api/client";
import {
  getRunStatusPresentation,
  isActiveRunStatus,
} from "@/features/command/composables/runPresentation";
import type { ModelStatusDTO, WorkerStatusDTO } from "@jarvis/shared";

const taskStore = useTaskStore();
const runStore = useRunStore();
const ui = useUiStore();
const workspaceStore = useWorkspaceStore();

// 3B: Worker status polling
const workers = ref<WorkerStatusDTO[]>([]);
const pollingError = ref(false);
let pollTimer: ReturnType<typeof setInterval> | null = null;

async function fetchWorkerStatus() {
  try {
    const result = await getWorkers();
    if (result.ok) {
      workers.value = result.data.workers;
      pollingError.value = false;
    } else {
      pollingError.value = true;
    }
  } catch {
    pollingError.value = true;
  }
}

onMounted(() => {
  workspaceStore.loadWorkspaces();
  fetchWorkerStatus();
  pollTimer = setInterval(fetchWorkerStatus, 5000);
});

onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
});

const onlineWorkers = computed(() =>
  workers.value.filter((w) => !w.is_stale)
);
const hasActiveRun = computed(() =>
  workers.value.some((w) => w.active_run_id)
);
const modelStatus = computed<{ label: string; textClass: string }>(() => {
  if (onlineWorkers.value.length === 0) {
    return { label: "Worker 未连接", textClass: "text-gray-400" };
  }

  const model: ModelStatusDTO | undefined =
    onlineWorkers.value.find((worker) => worker.model?.status === "configured")?.model
    ?? onlineWorkers.value.find((worker) => worker.model)?.model;
  if (!model || model.status !== "configured") {
    return { label: "Model 未配置", textClass: "text-amber-500" };
  }

  return {
    label: model.model_name || model.provider,
    textClass: "text-green-500",
  };
});

const runStatus = computed(() => {
  if (!taskStore.activeRunId) return null;
  return runStore.getStatus(taskStore.activeRunId);
});

const isRunActive = computed(() =>
  isActiveRunStatus(runStatus.value)
);

const statusText = computed(() => {
  if (!runStatus.value) return "就绪";
  return getRunStatusPresentation(runStatus.value).label;
});

const statusColor = computed(() => {
  if (!runStatus.value) return "text-gray-400";
  const tone = getRunStatusPresentation(runStatus.value).tone;
  if (tone === "info") return "text-blue-500";
  if (tone === "warning") return "text-amber-500";
  if (tone === "danger") return "text-red-500";
  if (tone === "success") return "text-green-500";
  return "text-gray-500";
});

const connectionStatus = computed(() => {
  if (pollingError.value) {
    return { label: "服务不可用", textClass: "text-red-500", icon: WifiOff };
  }
  if (taskStore.activeRunId && isRunActive.value) {
    const state = runStore.getConnectionState(taskStore.activeRunId);
    if (state === "connecting") {
      return { label: "事件连接中", textClass: "text-amber-500", icon: Wifi };
    }
    if (state === "reconnecting" || state === "closed") {
      return { label: "事件重连中", textClass: "text-amber-500", icon: WifiOff };
    }
  }
  if (workers.value.length === 0) {
    return { label: "正在检查", textClass: "text-gray-400", icon: Wifi };
  }
  if (onlineWorkers.value.length === 0) {
    return { label: "Worker 离线", textClass: "text-red-500", icon: WifiOff };
  }
  return { label: "服务正常", textClass: "text-green-500", icon: Wifi };
});

const inspectorOpen = computed(() =>
  ui.compactLayout ? ui.inspectorDrawerOpen : ui.inspectorVisible
);

const pickingWorkspace = ref(false);

function handleWorkspaceChange(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  workspaceStore.setSelectedWorkspaceId(value || null);
}

async function handlePickWorkspace() {
  if (pickingWorkspace.value) return;
  pickingWorkspace.value = true;
  try {
    await workspaceStore.pickAndAddWorkspace();
  } finally {
    pickingWorkspace.value = false;
  }
}
</script>

<template>
  <header
    class="flex h-11 shrink-0 items-center justify-between gap-1 border-b border-[var(--color-border)] bg-[var(--color-surface)] px-2 sm:gap-3 sm:px-4"
  >
    <!-- 左侧：运行状态 -->
    <div class="flex shrink-0 items-center gap-1.5 sm:gap-3">
      <button
        class="text-[var(--color-muted)] hover:text-[var(--color-text)] transition-colors"
        @click="ui.toggleSidebar()"
        title="切换侧栏"
      >
        <Activity :size="18" />
      </button>

      <div class="flex items-center gap-1.5 text-sm">
        <span class="hidden text-[var(--color-muted)] sm:inline">状态</span>
        <span :class="[statusColor, 'font-medium']">{{ statusText }}</span>
      </div>
    </div>

    <!-- 中间：工作区 -->
    <div class="flex min-w-0 flex-1 items-center justify-center gap-1 text-sm text-[var(--color-muted)] sm:gap-2">
      <span class="hidden shrink-0 text-xs md:inline">工作区</span>
      <select
        v-if="workspaceStore.activeWorkspaces.length > 0"
        :value="workspaceStore.selectedWorkspaceId ?? ''"
        :disabled="isRunActive"
        class="min-w-0 max-w-[120px] rounded border border-[var(--color-border)] bg-[var(--color-surface)] px-1.5 py-1 text-xs text-[var(--color-text)] disabled:opacity-60 sm:max-w-[190px] sm:px-2 lg:max-w-[260px]"
        title="选择下一次任务的工作区"
        @change="handleWorkspaceChange"
      >
        <option
          v-for="ws in workspaceStore.activeWorkspaces"
          :key="ws.id"
          :value="ws.id"
        >
          {{ workspaceStore.displayName(ws) }}
        </option>
      </select>
      <button
        :disabled="isRunActive || pickingWorkspace"
        class="shrink-0 p-1 rounded hover:bg-[var(--color-border)] transition-colors disabled:opacity-40"
        title="添加工作区"
        @click="handlePickWorkspace"
      >
        <FolderPlus :size="14" />
      </button>
      <span v-if="workspaceStore.loading" class="text-xs text-[var(--color-muted)]">
        加载中...
      </span>
      <span v-else-if="workspaceStore.error" class="text-xs text-red-400">
        {{ workspaceStore.error }}
      </span>
      <span v-else-if="workspaceStore.activeWorkspaces.length === 0 && !workspaceStore.loading" class="text-xs text-amber-500">
        未配置
      </span>
    </div>

    <!-- 右侧：系统状态 + Worker 状态 (3B) -->
    <div class="flex shrink-0 items-center gap-1.5 sm:gap-3">
      <!-- 3B: Worker status -->
      <div
        v-if="workers.length > 0"
        class="hidden items-center gap-1 text-xs sm:flex"
        :class="pollingError ? 'text-red-400' : 'text-green-500'"
        :title="'Workers: ' + workers.map(w => w.worker_id + ' (' + w.status + ')').join(', ')"
      >
        <Cpu :size="14" />
        <span>{{ onlineWorkers.length }}/{{ workers.length }}</span>
        <span v-if="hasActiveRun" class="text-blue-400 ml-0.5">●</span>
      </div>

      <div
        class="flex items-center gap-1 text-xs"
        :class="modelStatus.textClass"
        :title="'当前模型: ' + modelStatus.label"
      >
        <Zap :size="14" />
        <span class="hidden md:inline">{{ modelStatus.label }}</span>
      </div>
      <div
        class="flex items-center gap-1 text-xs"
        :class="connectionStatus.textClass"
        :title="connectionStatus.label"
      >
        <component :is="connectionStatus.icon" :size="14" />
        <span class="hidden xl:inline">{{ connectionStatus.label }}</span>
      </div>
      <button
        class="text-[var(--color-muted)] hover:text-[var(--color-text)] transition-colors"
        @click="ui.toggleInspector()"
        :title="inspectorOpen ? '隐藏检查器' : '显示检查器'"
      >
        <component :is="inspectorOpen ? WifiOff : Wifi" :size="16" />
      </button>
    </div>
  </header>
</template>
