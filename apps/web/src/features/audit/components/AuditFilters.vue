<script setup lang="ts">
import { reactive, watch } from "vue";
import type { ListAuditLogsInput } from "@jarvis/shared";
import { Filter, RefreshCw } from "@lucide/vue";

const props = defineProps<{ loading: boolean; initial: Omit<ListAuditLogsInput, "before"> }>();
const emit = defineEmits<{ apply: [filters: Omit<ListAuditLogsInput, "before">] }>();
const form = reactive<Omit<ListAuditLogsInput, "before">>({ limit: 50 });
watch(() => props.initial, (value) => Object.assign(form, value), { immediate: true, deep: true });

function apply() {
  emit("apply", {
    limit: form.limit || 50,
    event_type: form.event_type?.trim() || undefined,
    actor: form.actor?.trim() || undefined,
    task_id: form.task_id?.trim() || undefined,
    run_id: form.run_id?.trim() || undefined,
  });
}
</script>

<template>
  <form class="grid grid-cols-1 gap-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-3 md:grid-cols-6" @submit.prevent="apply">
    <input v-model="form.event_type" class="audit-input md:col-span-1" placeholder="事件类型" aria-label="事件类型" />
    <input v-model="form.actor" class="audit-input md:col-span-1" placeholder="执行者" aria-label="执行者" />
    <input v-model="form.task_id" class="audit-input md:col-span-1" placeholder="Task ID" aria-label="Task ID" />
    <input v-model="form.run_id" class="audit-input md:col-span-1" placeholder="Run ID" aria-label="Run ID" />
    <select v-model="form.limit" class="audit-input" aria-label="每页数量"><option :value="25">25 条</option><option :value="50">50 条</option><option :value="100">100 条</option></select>
    <button type="submit" class="inline-flex items-center justify-center gap-1 rounded bg-[var(--color-accent)] px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="loading">
      <RefreshCw v-if="loading" :size="14" class="animate-spin" /><Filter v-else :size="14" />
      {{ loading ? "查询中…" : "查询" }}
    </button>
  </form>
</template>

<style scoped>
.audit-input {
  min-width: 0;
  border: 1px solid var(--color-border);
  border-radius: 0.25rem;
  background: white;
  padding: 0.5rem 0.625rem;
  font-size: 0.75rem;
  color: var(--color-text);
  outline: none;
}
.audit-input:focus { border-color: rgb(59 130 246); }
</style>
