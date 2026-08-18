<script setup lang="ts">
// 主页面：Command Chat View — MVP 首屏
// 职责：组合布局，组装 ConversationThread + CommandComposer + RightInspector
// 真源：docs/11-frontend-app-ui-design.md § Command Chat View

import ConversationThread from "@/features/command/components/ConversationThread.vue";
import CommandComposer from "@/features/command/components/CommandComposer.vue";
import CommandRunStatus from "@/features/command/components/CommandRunStatus.vue";
import PermissionCard from "@/features/timeline/components/PermissionCard.vue";
import ArtifactCard from "@/features/artifact/components/ArtifactCard.vue";
import { normalizeArtifact } from "@/features/artifact/artifactContract";
import { useCommandSession } from "@/features/command/composables/useCommandSession";
import { findRetryableModelStepId } from "@/features/command/composables/retryableFailedStep";
import {
  getLatestRunError,
  getRunStatusPresentation,
  isActiveRunStatus,
} from "@/features/command/composables/runPresentation";
import { useTaskStore } from "@/stores/taskStore";
import { useRunStore } from "@/stores/runStore";
import { useUiStore } from "@/stores/uiStore";
import { usePermissionStore } from "@/stores/permissionStore";
import { cancelRun, pauseRun, resumeRun, retryFailedStep } from "@/api/client";
import { normalizeClientError } from "@/api/errors";
import { computed, ref, watch } from "vue";
import { Pause as PauseIcon, Play as PlayIcon, XCircle } from "@lucide/vue";
import type { AppError, ArtifactDTO } from "@jarvis/shared";

const taskStore = useTaskStore();
const runStore = useRunStore();
const ui = useUiStore();
const permissionStore = usePermissionStore();
const { isSubmitting, error, submit, retry: retrySubmit, clearError } = useCommandSession();

// 3C: cancel 状态
const isCancelling = ref(false);
const isPausing = ref(false);
const isResuming = ref(false);
const pauseSubmitted = ref(false);
const resumeSubmitted = ref(false);
const isRetrying = ref(false);
const operationError = ref<AppError | null>(null);

const activeStatus = computed(() => {
  const runId = taskStore.activeRunId;
  return runId ? runStore.getStatus(runId) : null;
});

const statusPresentation = computed(() =>
  activeStatus.value ? getRunStatusPresentation(activeStatus.value) : null
);

const eventConnectionState = computed(() => {
  const runId = taskStore.activeRunId;
  return runId ? runStore.getConnectionState(runId) : "closed";
});

const activeRunError = computed(() => {
  const runId = taskStore.activeRunId;
  return runId ? getLatestRunError(runStore.getEvents(runId)) : null;
});

const activePendingPermissions = computed(() => {
  const runId = taskStore.activeRunId;
  return runId ? permissionStore.getPendingByRun(runId) : [];
});

const activeResolvedPermissions = computed(() => {
  const runId = taskStore.activeRunId;
  if (!runId || activePendingPermissions.value.length > 0) return [];
  return permissionStore.getResolvedByRun(runId).slice(0, 1);
});

const retryableModelStepId = computed(() => {
  const runId = taskStore.activeRunId;
  if (!runId) return null;
  return findRetryableModelStepId(activeStatus.value, runStore.getEvents(runId));
});

const activeDeliverables = computed<ArtifactDTO[]>(() => {
  const runId = taskStore.activeRunId;
  if (!runId) return [];
  const byId = new Map<string, ArtifactDTO>();
  for (const event of runStore.getEvents(runId)) {
    if (event.type !== "artifact.created") continue;
    const artifact = (
      event.payload as {
        artifact?: ArtifactDTO | (Omit<ArtifactDTO, "purpose" | "producer"> & {
          purpose?: ArtifactDTO["purpose"];
          producer?: ArtifactDTO["producer"];
        });
      }
    ).artifact;
    if (!artifact?.id) continue;
    const normalized = normalizeArtifact(artifact);
    if (normalized.purpose === "deliverable") {
      byId.set(normalized.id, normalized);
    }
  }
  return [...byId.values()];
});

watch(activeStatus, (status) => {
  if (status === "paused" || status === "completed" || status === "failed" || status === "cancelled") {
    pauseSubmitted.value = false;
  }
  if (status === "running" || status === "completed" || status === "failed" || status === "cancelled") {
    resumeSubmitted.value = false;
  }
});

watch(() => taskStore.activeRunId, () => {
  operationError.value = null;
  clearError();
  isRetrying.value = false;
});

/** 以 active run status 为准，不在运行中即可输入 */
const isRunning = computed(() => {
  const runId = taskStore.activeRunId;
  if (!runId) return false;
  return isActiveRunStatus(activeStatus.value);
});

async function handleSubmit(text: string) {
  if (await submit(text)) {
    ui.composerDraft = "";
  }
}

async function handleRetrySubmit() {
  if (await retrySubmit()) {
    ui.composerDraft = "";
  }
}

function setOperationError(error: unknown, fallback: string) {
  operationError.value = normalizeClientError(error, fallback);
}

/** 3C: cancel 当前 active run */
async function handleCancel() {
  const runId = taskStore.activeRunId;
  if (!runId) return;
  isCancelling.value = true;
  operationError.value = null;
  try {
    const result = await cancelRun(runId);
    if (!result.ok) {
      operationError.value = result.error;
    }
    // 不提前修改前端状态 — 最终 cancelled 来自 agent.run.cancelled RuntimeEvent
  } catch (cause) {
    setOperationError(cause, "取消失败，请重试");
  } finally {
    isCancelling.value = false;
  }
}

async function handlePause() {
  const runId = taskStore.activeRunId;
  if (!runId) return;
  isPausing.value = true;
  operationError.value = null;
  try {
    const result = await pauseRun(runId);
    if (!result.ok) operationError.value = result.error;
    else pauseSubmitted.value = true;
  } catch (cause) {
    setOperationError(cause, "暂停失败，请重试");
  } finally {
    isPausing.value = false;
  }
}

async function handleResume() {
  const runId = taskStore.activeRunId;
  if (!runId) return;
  isResuming.value = true;
  operationError.value = null;
  try {
    const result = await resumeRun(runId);
    if (!result.ok) operationError.value = result.error;
    else resumeSubmitted.value = true;
  } catch (cause) {
    setOperationError(cause, "恢复失败，请重试");
  } finally {
    isResuming.value = false;
  }
}

async function handleRetryFailedStep() {
  const runId = taskStore.activeRunId;
  const stepId = retryableModelStepId.value;
  if (!runId || !stepId) return;
  isRetrying.value = true;
  operationError.value = null;
  try {
    const result = await retryFailedStep(runId, stepId);
    if (!result.ok) operationError.value = result.error;
    else taskStore.activateReplacementRun(result.data.id);
  } catch (cause) {
    setOperationError(cause, "恢复运行创建失败，请重试");
  } finally {
    isRetrying.value = false;
  }
}

function handleReconnect() {
  const runId = taskStore.activeRunId;
  if (runId) runStore.resubscribe(runId);
}
</script>

<template>
  <div class="flex min-w-0 flex-col h-full">
    <!-- 对话线程 -->
    <ConversationThread />

    <section v-if="activeDeliverables.length" class="min-w-0 space-y-2 border-t border-[var(--color-border)] px-2 py-3 sm:px-4">
      <p class="text-xs font-medium uppercase tracking-wide text-gray-500">交付物</p>
      <ArtifactCard v-for="artifact in activeDeliverables" :key="artifact.id" :artifact="artifact" />
    </section>

    <!-- 权限接管区固定在 Command Center 底部，避免被长 Timeline 推离视口。 -->
    <section
      v-if="activePendingPermissions.length || activeResolvedPermissions.length"
      class="max-h-[64vh] min-w-0 shrink-0 overflow-y-auto border-t border-amber-200 bg-white px-2 sm:px-4"
      aria-label="当前权限接管"
    >
      <div class="mx-auto max-w-3xl">
        <PermissionCard
          v-for="request in activePendingPermissions"
          :key="request.id"
          :request="request"
        />
        <PermissionCard
          v-for="request in activeResolvedPermissions"
          :key="'resolved-' + request.id"
          :request="request"
        />
      </div>
    </section>

    <CommandRunStatus
      v-if="taskStore.activeRunId && statusPresentation"
      :presentation="statusPresentation"
      :connection-state="eventConnectionState"
      :run-error="activeRunError"
      :operation-error="operationError"
      :can-retry-step="Boolean(retryableModelStepId)"
      :can-reconnect="isRunning"
      :retrying="isRetrying"
      @retry-step="handleRetryFailedStep"
      @reconnect="handleReconnect"
      @dismiss-error="operationError = null"
    />

    <!-- 创建任务失败时保留草稿并提供原地重试 -->
    <div
      v-if="error && !taskStore.activeRunId"
      class="flex min-w-0 flex-col items-stretch gap-2 border-t border-red-100 bg-red-50 px-2 py-2 text-sm text-red-700 sm:flex-row sm:items-center sm:gap-3 sm:px-4"
      role="alert"
    >
      <div class="min-w-0 flex-1">
        <p class="break-words [overflow-wrap:anywhere]">{{ error.message }}</p>
        <p class="mt-0.5 break-words text-xs opacity-70 [overflow-wrap:anywhere]">
          错误码 {{ error.code }} · 输入内容已保留
        </p>
      </div>
      <div class="flex shrink-0 justify-end gap-2">
        <button
          v-if="error.recoverable"
          class="shrink-0 rounded border border-red-300 bg-white px-2 py-1 text-xs hover:bg-red-100 disabled:opacity-50"
          :disabled="isSubmitting"
          @click="handleRetrySubmit"
        >
          {{ isSubmitting ? "重试中..." : "重新提交" }}
        </button>
        <button class="shrink-0 px-1 text-xs opacity-70" @click="clearError">关闭</button>
      </div>
    </div>

    <!-- active run 控制：状态只由后端事件确认 -->
    <div
      v-if="isRunning && taskStore.activeRunId"
      class="flex flex-wrap items-center justify-end gap-2 border-t border-[var(--color-border)] bg-gray-50 px-2 py-1.5 sm:px-4"
    >
      <button
        v-if="activeStatus === 'running'"
        class="text-xs px-2 py-0.5 rounded border border-sky-300 text-sky-700 hover:bg-sky-50 transition-colors flex items-center gap-1 disabled:opacity-50"
        :disabled="isPausing || pauseSubmitted"
        @click="handlePause"
      >
        <PauseIcon :size="12" />
        {{ pauseSubmitted ? "等待安全暂停..." : isPausing ? "请求中..." : "暂停运行" }}
      </button>
      <button
        v-if="activeStatus === 'paused'"
        class="text-xs px-2 py-0.5 rounded border border-emerald-300 text-emerald-700 hover:bg-emerald-50 transition-colors flex items-center gap-1 disabled:opacity-50"
        :disabled="isResuming || resumeSubmitted"
        @click="handleResume"
      >
        <PlayIcon :size="12" />
        {{ resumeSubmitted ? "等待恢复..." : isResuming ? "恢复中..." : "恢复运行" }}
      </button>
      <button
        class="text-xs px-2 py-0.5 rounded border border-amber-300 text-amber-700 hover:bg-amber-50 transition-colors flex items-center gap-1"
        :disabled="isCancelling"
        @click="handleCancel"
      >
        <XCircle :size="12" />
        {{ isCancelling ? "取消中..." : "取消运行" }}
      </button>
    </div>

    <!-- 底部输入区 -->
    <CommandComposer
      v-model="ui.composerDraft"
      :loading="isSubmitting"
      :disabled="isRunning"
      @submit="handleSubmit"
    />
  </div>
</template>
