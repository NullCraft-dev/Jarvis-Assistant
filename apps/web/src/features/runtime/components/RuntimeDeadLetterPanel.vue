<script setup lang="ts">
import { reactive, watch } from "vue";
import type { ListRuntimeDeadLettersInput, RuntimeDeadLetterDTO } from "@jarvis/shared";
import { Filter, Inbox, RefreshCw } from "@lucide/vue";

const props = defineProps<{
  records: RuntimeDeadLetterDTO[];
  filters: Omit<ListRuntimeDeadLettersInput, "before">;
  loading: boolean;
  loadingMore: boolean;
  nextCursor: string | null;
  error: string | null;
}>();
const emit = defineEmits<{
  apply: [filters: Omit<ListRuntimeDeadLettersInput, "before">];
  loadMore: [];
  inspectRetry: [record: RuntimeDeadLetterDTO];
}>();
const form = reactive<Omit<ListRuntimeDeadLettersInput, "before">>({ source: "run_queue", limit: 20 });
watch(() => props.filters, (value) => Object.assign(form, value), { immediate: true, deep: true });
function apply() {
  emit("apply", {
    source: form.source,
    limit: form.limit ?? 20,
    error_code: form.error_code?.trim().toUpperCase() || undefined,
    task_id: form.task_id?.trim() || undefined,
    run_id: form.run_id?.trim() || undefined,
  });
}
function shortHash(value: string) { return value.length > 16 ? `${value.slice(0, 12)}…` : value || "—"; }
function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString();
}
</script>

<template>
  <section class="overflow-hidden rounded-lg border border-[var(--color-border)] bg-white">
    <div class="border-b border-[var(--color-border)] px-4 py-3"><div class="flex items-center gap-2"><Inbox :size="16" class="text-[var(--color-muted)]" /><h2 class="text-sm font-medium text-[var(--color-text)]">Dead-letter 诊断记录</h2></div><p class="mt-1 text-xs text-[var(--color-muted)]">只显示脱敏白名单字段。原始 payload 不可见，也不能从这里重放或删除。</p></div>
    <form class="grid gap-2 border-b border-[var(--color-border)] bg-gray-50 p-3 md:grid-cols-6" @submit.prevent="apply">
      <select v-model="form.source" class="runtime-filter" aria-label="DLQ 链路"><option value="run_queue">Run Queue</option><option value="worker_command">Worker Command</option><option value="runtime_event">Runtime Event</option></select>
      <input v-model="form.error_code" class="runtime-filter" placeholder="错误码" aria-label="DLQ 错误码" />
      <input v-model="form.task_id" class="runtime-filter" placeholder="Task ID" aria-label="DLQ Task ID" />
      <input v-model="form.run_id" class="runtime-filter" placeholder="Run ID" aria-label="DLQ Run ID" />
      <select v-model="form.limit" class="runtime-filter" aria-label="DLQ 每页数量"><option :value="10">10 条</option><option :value="20">20 条</option><option :value="50">50 条</option></select>
      <button type="submit" class="inline-flex items-center justify-center gap-1 rounded bg-[var(--color-accent)] px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="loading"><RefreshCw v-if="loading" :size="14" class="animate-spin" /><Filter v-else :size="14" />{{ loading ? "查询中…" : "查询" }}</button>
    </form>
    <div v-if="error" class="border-b border-red-200 bg-red-50 px-4 py-2 text-xs text-red-600">{{ error }}</div>
    <div v-if="records.length" class="overflow-x-auto">
      <table class="w-full min-w-[1020px] text-left text-xs">
        <thead class="text-[var(--color-muted)]"><tr><th class="px-4 py-2.5 font-medium">失败时间</th><th class="px-3 py-2.5 font-medium">错误</th><th class="px-3 py-2.5 font-medium">关联对象</th><th class="px-3 py-2.5 font-medium">交付</th><th class="px-3 py-2.5 font-medium">Payload 指纹</th><th class="px-3 py-2.5 font-medium">处置</th></tr></thead>
        <tbody class="divide-y divide-[var(--color-border)]">
          <tr v-for="record in records" :key="record.id" class="align-top">
            <td class="whitespace-nowrap px-4 py-3 text-[var(--color-muted)]">{{ formatDate(record.failed_at) }}</td>
            <td class="max-w-[320px] px-3 py-3"><div class="font-mono text-[11px] font-medium text-red-600">{{ record.error_code || "UNKNOWN" }}</div><p class="mt-1 break-words text-[var(--color-muted)]">{{ record.error_message || "无诊断摘要" }}</p></td>
            <td class="px-3 py-3 font-mono text-[10px] text-[var(--color-muted)]"><div>Task: {{ record.task_id || "—" }}</div><div class="mt-1">Run: {{ record.run_id || "—" }}</div><div class="mt-1">Message: {{ record.original_message_id }}</div></td>
            <td class="px-3 py-3 tabular-nums"><div>{{ record.delivery_count }} 次</div><div class="mt-1 text-[var(--color-muted)]">{{ record.reclaimed ? "reclaimed" : "首次交付" }}</div></td>
            <td class="px-3 py-3 font-mono text-[10px] text-[var(--color-muted)]"><div :title="record.payload_sha256">{{ shortHash(record.payload_sha256) }}</div><div class="mt-1">{{ record.payload_size_bytes }} bytes</div></td>
            <td class="px-3 py-3"><button class="whitespace-nowrap rounded border border-[var(--color-border)] px-2.5 py-1.5 text-[11px] text-[var(--color-text)] hover:bg-gray-50" @click="emit('inspectRetry', record)">检查处置</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else-if="!loading" class="px-4 py-8 text-center text-xs text-[var(--color-muted)]">当前筛选条件下没有 DLQ 记录</div>
    <div v-if="nextCursor" class="border-t border-[var(--color-border)] p-3"><button class="w-full rounded border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-muted)] hover:bg-gray-50 disabled:opacity-50" :disabled="loadingMore" @click="emit('loadMore')">{{ loadingMore ? "加载中…" : "加载更多" }}</button></div>
  </section>
</template>

<style scoped>
.runtime-filter { min-width: 0; border: 1px solid var(--color-border); border-radius: .25rem; background: white; padding: .5rem .625rem; font-size: .75rem; color: var(--color-text); outline: none; }
.runtime-filter:focus { border-color: rgb(59 130 246); }
</style>
