<script setup lang="ts">
// 单个时间线步骤节点
// 真源：docs/11-frontend-app-ui-design.md § Timeline Item

import type { RuntimeEvent } from "@jarvis/shared";
import {
  getRuntimeEventPresentation,
  type TimelineDetailTarget,
} from "@/features/timeline/runtimeEventPresentation";
import {
  MessageSquare,
  Brain,
  Wrench,
  Shield,
  FileText,
  CheckCircle,
  XCircle,
  Loader2,
  Ban,
} from "@lucide/vue";
import { computed } from "vue";

const props = defineProps<{
  event: RuntimeEvent;
}>();

const emit = defineEmits<{
  inspect: [target: TimelineDetailTarget];
}>();

const icon = computed(() => {
  switch (props.event.type) {
    case "agent.step.started":
      return Loader2;
    case "agent.step.completed":
      return CheckCircle;
    case "agent.step.failed":
      return XCircle;
    case "model.call.started":
    case "model.delta":
    case "model.call.completed":
    case "model.call.failed":
      return Brain;
    case "tool.call.started":
    case "tool.call.finished":
    case "tool.call.failed":
      return Wrench;
    case "permission.required":
    case "permission.resolved":
    case "permission.expired":
      return Shield;
    case "artifact.created":
      return FileText;
    case "agent.run.completed":
      return CheckCircle;
    case "agent.run.failed":
      return XCircle;
    case "agent.run.cancelled":
      return Ban;
    default:
      return MessageSquare;
  }
});

const presentation = computed(() => getRuntimeEventPresentation(props.event));

const isRunning = computed(() =>
  ["agent.step.started", "model.call.started", "tool.call.started"].includes(
    props.event.type
  )
);

const isError = computed(() =>
  ["agent.step.failed", "tool.call.failed", "agent.run.failed"].includes(
    props.event.type
  )
);

const isCancelled = computed(() => props.event.type === "agent.run.cancelled");
</script>

<template>
  <div class="flex gap-2 border-b border-gray-100 py-2.5 text-sm last:border-b-0">
    <!-- 左侧图标 -->
    <div class="shrink-0 mt-0.5">
      <component
        :is="icon"
        :size="16"
        :class="[
          isRunning ? 'text-blue-500 animate-spin' : '',
          isError ? 'text-red-500' : '',
          isCancelled ? 'text-amber-500' : '',
          !isRunning && !isError && !isCancelled ? 'text-[var(--color-muted)]' : '',
        ]"
      />
    </div>

    <!-- 内容 -->
    <div class="flex-1 min-w-0">
      <div class="flex flex-wrap items-center gap-2">
        <span
          class="font-medium text-[var(--color-text)]"
          :class="{ 'text-red-600': isError }"
        >
          {{ presentation.title }}
        </span>
        <span class="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-[var(--color-muted)]">
          {{ presentation.categoryLabel }}
        </span>
      </div>
      <div
        v-if="presentation.summary"
        class="mt-1 break-words text-xs leading-5 text-[var(--color-muted)]"
      >
        {{ presentation.summary }}
      </div>
      <div class="mt-1 flex items-center justify-between gap-2">
        <span class="text-[11px] text-gray-400">
          {{ new Date(event.timestamp).toLocaleTimeString() }}
        </span>
        <button
          v-if="presentation.detailTarget"
          class="hidden text-[11px] text-[var(--color-accent)] hover:underline lg:inline"
          @click="emit('inspect', presentation.detailTarget)"
        >
          在检查器中查看
        </button>
      </div>
    </div>
  </div>
</template>
