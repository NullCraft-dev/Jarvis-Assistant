<script setup lang="ts">
import type { AuditLogDTO } from "@jarvis/shared";
import { ShieldCheck } from "@lucide/vue";
defineProps<{ log: AuditLogDTO | null }>();
</script>

<template>
  <aside class="min-w-0 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
    <div v-if="log" class="space-y-4">
      <div class="flex items-center gap-2"><ShieldCheck :size="17" class="text-blue-600" /><h2 class="text-sm font-medium text-[var(--color-text)]">审计详情</h2></div>
      <dl class="space-y-2 text-xs"><div><dt class="audit-label">操作</dt><dd>{{ log.action_summary }}</dd></div><div><dt class="audit-label">事件 / 执行者</dt><dd>{{ log.event_type }} / {{ log.actor }}</dd></div><div v-if="log.result_summary"><dt class="audit-label">结果摘要</dt><dd>{{ log.result_summary }}</dd></div><div v-if="log.error_code"><dt class="audit-label">安全错误码</dt><dd class="text-red-600">{{ log.error_code }}</dd></div><div v-if="log.task_id"><dt class="audit-label">Task</dt><dd class="break-all font-mono">{{ log.task_id }}</dd></div><div v-if="log.run_id"><dt class="audit-label">Run</dt><dd class="break-all font-mono">{{ log.run_id }}</dd></div></dl>
      <div><p class="audit-label">安全详情摘要</p><pre class="max-h-64 overflow-auto rounded bg-gray-50 p-2 text-[11px] text-[var(--color-text)]">{{ JSON.stringify(log.details_summary, null, 2) }}</pre></div>
    </div>
    <div v-else class="py-12 text-center text-sm text-[var(--color-muted)]">选择一条审计记录查看安全详情</div>
  </aside>
</template>

<style scoped>
.audit-label { margin-bottom: 0.125rem; color: var(--color-muted); }
</style>
