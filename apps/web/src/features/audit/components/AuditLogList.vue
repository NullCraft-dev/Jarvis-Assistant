<script setup lang="ts">
import type { AuditLogDTO } from "@jarvis/shared";
import RiskBadge from "@/components/ui/RiskBadge.vue";
import { AlertCircle, CheckCircle2, ChevronRight } from "@lucide/vue";

defineProps<{ logs: AuditLogDTO[]; selectedId: string | null }>();
const emit = defineEmits<{ select: [log: AuditLogDTO] }>();
</script>

<template>
  <div class="overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]">
    <button v-for="log in logs" :key="log.id" class="grid w-full grid-cols-[auto_1fr_auto] items-center gap-3 border-b border-[var(--color-border)] px-4 py-3 text-left last:border-b-0 hover:bg-gray-50" :class="{ 'bg-blue-50': selectedId === log.id }" @click="emit('select', log)">
      <AlertCircle v-if="log.error_code" :size="16" class="text-red-500" /><CheckCircle2 v-else :size="16" class="text-emerald-500" />
      <span class="min-w-0"><span class="block truncate text-sm font-medium text-[var(--color-text)]">{{ log.action_summary }}</span><span class="mt-0.5 block truncate text-xs text-[var(--color-muted)]">{{ log.event_type }} · {{ log.actor }} · {{ new Date(log.created_at).toLocaleString() }}</span></span>
      <span class="flex items-center gap-2"><RiskBadge v-if="log.risk_level" :level="log.risk_level" /><ChevronRight :size="15" class="text-[var(--color-muted)]" /></span>
    </button>
    <div v-if="logs.length === 0" class="px-4 py-12 text-center text-sm text-[var(--color-muted)]">没有符合条件的审计记录</div>
  </div>
</template>
