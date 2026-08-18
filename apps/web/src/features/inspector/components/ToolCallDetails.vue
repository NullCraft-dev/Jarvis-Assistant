<script setup lang="ts">
import RiskBadge from "@/components/ui/RiskBadge.vue";
import type { ToolCallView } from "@/features/inspector/composables/toolCallView";
import { CheckCircle, Loader2, XCircle } from "@lucide/vue";
import { computed } from "vue";

const props = defineProps<{ call: ToolCallView }>();

const statusLabel = computed(() => ({
  running: "执行中",
  completed: "已完成",
  failed: "失败",
}[props.call.status]));

const permissionLabel = computed(() => ({
  not_required: "",
  pending: "",
  approved: "已授权",
  denied: "已拒绝",
  expired: "授权已过期",
}[props.call.permissionStatus]));

const formattedArguments = computed(() => {
  const text = JSON.stringify(props.call.argumentsSummary, null, 2);
  return text.length > 2000 ? `${text.slice(0, 2000)}\n…` : text;
});

const formattedDuration = computed(() => {
  if (props.call.durationMs === undefined) return "";
  return props.call.durationMs < 1000
    ? `${props.call.durationMs} ms`
    : `${(props.call.durationMs / 1000).toFixed(2)} s`;
});
</script>

<template>
  <article class="rounded-lg border border-[var(--color-border)] bg-white p-3 text-sm">
    <div class="flex items-start justify-between gap-2">
      <div class="min-w-0">
        <div class="font-medium text-[var(--color-text)] truncate">{{ call.toolName }}</div>
        <div class="mt-0.5 text-[11px] text-[var(--color-muted)]">
          {{ call.provider }}<span v-if="permissionLabel"> · {{ permissionLabel }}</span><span v-if="formattedDuration"> · {{ formattedDuration }}</span>
        </div>
      </div>
      <div class="flex items-center gap-1.5 shrink-0">
        <RiskBadge :level="call.riskLevel" />
        <span
          class="inline-flex items-center gap-1 text-xs"
          :class="call.status === 'failed' ? 'text-red-600' : call.status === 'completed' ? 'text-green-600' : 'text-blue-600'"
        >
          <Loader2 v-if="call.status === 'running'" :size="12" class="animate-spin" />
          <CheckCircle v-else-if="call.status === 'completed'" :size="12" />
          <XCircle v-else :size="12" />
          {{ statusLabel }}
        </span>
      </div>
    </div>

    <div v-if="call.resultSummary" class="mt-2 text-xs text-[var(--color-text)]">
      {{ call.resultSummary }}
    </div>

    <div v-if="call.error" class="mt-2 rounded bg-red-50 p-2 text-xs text-red-700">
      <div class="font-mono font-medium">{{ call.error.code }}</div>
      <div class="mt-0.5">{{ call.error.message }}</div>
    </div>

    <details v-if="Object.keys(call.argumentsSummary).length" class="mt-2">
      <summary class="cursor-pointer text-xs text-[var(--color-muted)]">参数摘要</summary>
      <pre class="mt-1 overflow-x-auto whitespace-pre-wrap break-all rounded bg-gray-50 p-2 text-[11px] text-gray-600">{{ formattedArguments }}</pre>
    </details>

    <details v-if="call.contentPreview" class="mt-2">
      <summary class="cursor-pointer text-xs text-[var(--color-muted)]">读取内容预览</summary>
      <pre class="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-2 text-[11px] text-gray-600">{{ call.contentPreview }}</pre>
    </details>
  </article>
</template>
