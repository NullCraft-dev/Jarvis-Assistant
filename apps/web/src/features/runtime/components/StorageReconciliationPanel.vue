<script setup lang="ts">
import type { StorageReconciliationDTO, StorageReconciliationIssueDTO } from "@jarvis/shared";
import { AlertTriangle, CheckCircle2, Database, LoaderCircle } from "@lucide/vue";

defineProps<{
  result: StorageReconciliationDTO | null;
  loading: boolean;
  error: string | null;
}>();
const emit = defineEmits<{
  inspectRepair: [issue: StorageReconciliationIssueDTO];
}>();

function formatDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString();
}
</script>

<template>
  <section class="overflow-hidden rounded-lg border border-[var(--color-border)] bg-white">
    <div class="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--color-border)] px-4 py-3">
      <div>
        <div class="flex items-center gap-2">
          <Database :size="16" class="text-[var(--color-muted)]" />
          <h2 class="text-sm font-medium text-[var(--color-text)]">PostgreSQL 业务真源对账</h2>
        </div>
        <p class="mt-1 text-xs text-[var(--color-muted)]">只读检查最近 Run、事件、步骤与 Artifact 的关联关系；发现问题也不会自动修复或修改运行数据。</p>
      </div>
      <div v-if="result" class="flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs" :class="result.status === 'healthy' ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'">
        <CheckCircle2 v-if="result.status === 'healthy'" :size="14" />
        <AlertTriangle v-else :size="14" />
        {{ result.status === "healthy" ? "一致" : `${result.issue_count} 个异常` }}
      </div>
    </div>

    <div v-if="error" class="border-b border-red-200 bg-red-50 px-4 py-2 text-xs text-red-600">{{ error }}</div>
    <div v-if="loading && !result" class="flex items-center justify-center gap-2 px-4 py-8 text-xs text-[var(--color-muted)]">
      <LoaderCircle :size="15" class="animate-spin" />正在对账…
    </div>

    <template v-else-if="result">
      <div class="grid grid-cols-2 gap-px border-b border-[var(--color-border)] bg-[var(--color-border)] sm:grid-cols-4">
        <div class="bg-white px-4 py-3"><div class="text-lg font-medium tabular-nums">{{ result.scanned_runs }}</div><div class="text-[11px] text-[var(--color-muted)]">Run</div></div>
        <div class="bg-white px-4 py-3"><div class="text-lg font-medium tabular-nums">{{ result.scanned_events }}</div><div class="text-[11px] text-[var(--color-muted)]">RuntimeEvent</div></div>
        <div class="bg-white px-4 py-3"><div class="text-lg font-medium tabular-nums">{{ result.scanned_steps }}</div><div class="text-[11px] text-[var(--color-muted)]">ExecutionStep</div></div>
        <div class="bg-white px-4 py-3"><div class="text-lg font-medium tabular-nums">{{ result.scanned_artifacts }}</div><div class="text-[11px] text-[var(--color-muted)]">Artifact</div></div>
      </div>

      <div v-if="result.status === 'healthy'" class="flex items-center gap-2 px-4 py-6 text-sm text-emerald-700">
        <CheckCircle2 :size="17" />最近 {{ result.scanned_runs }} 个 Run 未发现一致性异常。
      </div>
      <div v-else class="overflow-x-auto">
        <table class="w-full min-w-[760px] text-left text-xs">
          <thead class="text-[var(--color-muted)]"><tr><th class="px-4 py-2.5 font-medium">级别</th><th class="px-3 py-2.5 font-medium">检查项</th><th class="px-3 py-2.5 font-medium">说明</th><th class="px-3 py-2.5 font-medium">关联对象</th><th class="px-3 py-2.5 font-medium">处置</th></tr></thead>
          <tbody class="divide-y divide-[var(--color-border)]">
            <tr v-for="(issue, index) in result.issues" :key="`${issue.code}-${issue.entity_id}-${index}`" class="align-top">
              <td class="px-4 py-3"><span class="rounded px-1.5 py-0.5" :class="issue.severity === 'error' ? 'bg-red-50 text-red-700' : 'bg-amber-50 text-amber-700'">{{ issue.severity === "error" ? "错误" : "提醒" }}</span></td>
              <td class="px-3 py-3 font-mono text-[11px] text-[var(--color-text)]">{{ issue.code }}</td>
              <td class="px-3 py-3 text-[var(--color-muted)]">{{ issue.summary }}</td>
              <td class="px-3 py-3 font-mono text-[10px] text-[var(--color-muted)]"><div>{{ issue.entity_type }}: {{ issue.entity_id }}</div><div v-if="issue.task_id" class="mt-1">Task: {{ issue.task_id }}</div><div v-if="issue.run_id" class="mt-1">Run: {{ issue.run_id }}</div></td>
              <td class="px-3 py-3"><button v-if="issue.code === 'TERMINAL_EVENT_MISSING' && issue.run_id" class="whitespace-nowrap rounded border border-amber-200 px-2.5 py-1.5 text-[11px] text-amber-700 hover:bg-amber-50" @click="emit('inspectRepair', issue)">检查修复</button><span v-else class="text-[10px] text-[var(--color-muted)]">仅诊断</span></td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="flex flex-wrap justify-between gap-2 border-t border-[var(--color-border)] px-4 py-2 text-[10px] text-[var(--color-muted)]">
        <span v-if="result.truncated" class="text-amber-700">当前为有界快照；更早数据可能未扫描，或部分异常未展示。</span>
        <span v-else>检查结果完整</span>
        <span>快照时间 {{ formatDate(result.generated_at) }}</span>
      </div>
    </template>
  </section>
</template>
