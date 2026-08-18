<script setup lang="ts">
// 对话流中嵌入的运行块，展示逐步出现的 Timeline 事件
// 真源：docs/11-frontend-app-ui-design.md § InlineRunBlock

import { computed, ref, watch } from "vue";
import type { ID } from "@jarvis/shared";
import { useRunStore } from "@/stores/runStore";
import { useUiStore } from "@/stores/uiStore";
import { useTaskStore } from "@/stores/taskStore";
import TimelineStep from "./TimelineStep.vue";
import {
  getRunStatusPresentation,
  isActiveRunStatus,
} from "@/features/command/composables/runPresentation";
import {
  buildTimelineEvents,
  summarizeTimeline,
  type TimelineDetailTarget,
} from "@/features/timeline/runtimeEventPresentation";
import { ChevronDown, ChevronRight, Loader2 } from "@lucide/vue";

const props = defineProps<{
  runId: ID;
}>();

const runStore = useRunStore();
const ui = useUiStore();
const taskStore = useTaskStore();

const events = computed(() => runStore.getEvents(props.runId));
const status = computed(() => runStore.getStatus(props.runId));
const isActive = computed(() => isActiveRunStatus(status.value));
const statusPresentation = computed(() => getRunStatusPresentation(status.value));
const statusBadgeClass = computed(() => {
  switch (statusPresentation.value.tone) {
    case "info":
      return "bg-blue-50 text-blue-700";
    case "warning":
      return "bg-amber-50 text-amber-700";
    case "success":
      return "bg-emerald-50 text-emerald-700";
    case "danger":
      return "bg-red-50 text-red-700";
    default:
      return "bg-gray-100 text-gray-600";
  }
});
const timelineEvents = computed(() => buildTimelineEvents(events.value));
const timelineSummary = computed(() => summarizeTimeline(events.value));
// 新创建的本地 Run 自动展开；历史 Run 恢复时保持折叠，避免事件回放顺序导致整页撑开。
const expanded = ref(taskStore.localPresentationRunId === props.runId);

watch(status, (nextStatus, previousStatus) => {
  if (
    nextStatus !== previousStatus &&
    ["completed", "failed", "cancelled"].includes(nextStatus)
  ) {
    expanded.value = false;
  }
});

function inspect(target: TimelineDetailTarget) {
  ui.openInspector(target);
}
</script>

<template>
  <div
    class="my-3 border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] overflow-hidden"
  >
    <!-- 运行块头部 -->
    <button
      class="w-full flex items-center justify-between px-3 py-2 hover:bg-gray-50 transition-colors"
      @click="expanded = !expanded"
    >
      <div class="flex min-w-0 items-center gap-2">
        <Loader2 v-if="isActive" :size="14" class="text-blue-500 animate-spin" />
        <div class="min-w-0 text-left">
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-[var(--color-text)]">执行过程</span>
            <span
              class="rounded px-1.5 py-0.5 text-[11px] font-medium"
              :class="statusBadgeClass"
            >
              {{ statusPresentation.label }}
            </span>
          </div>
          <p class="mt-0.5 truncate text-[11px] text-[var(--color-muted)]">
            {{ timelineSummary }}
          </p>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <span class="hidden text-xs text-[var(--color-muted)] sm:inline">过程摘要</span>
        <component :is="expanded ? ChevronDown : ChevronRight" :size="16" class="text-[var(--color-muted)]" />
      </div>
    </button>

    <!-- 展开的事件列表 -->
    <div v-if="expanded" class="px-3 pb-2 border-t border-[var(--color-border)]">
      <TimelineStep
        v-for="event in timelineEvents"
        :key="event.id"
        :event="event"
        @inspect="inspect"
      />

      <!-- 空状态 -->
      <div
        v-if="timelineEvents.length === 0"
        class="py-4 text-center text-xs text-[var(--color-muted)]"
      >
        <Loader2 :size="14" class="animate-spin inline mr-1" />
        等待事件...
      </div>
    </div>
  </div>
</template>
