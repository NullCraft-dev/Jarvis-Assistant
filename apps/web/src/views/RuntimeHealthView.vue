<script setup lang="ts">
import { onMounted } from "vue";
import { Activity, AlertCircle, RefreshCw } from "@lucide/vue";
import { useRuntimeHealthStore } from "@/stores/runtimeHealthStore";
import RuntimeSummary from "@/features/runtime/components/RuntimeSummary.vue";
import RuntimeStreamTable from "@/features/runtime/components/RuntimeStreamTable.vue";
import RuntimeCounters from "@/features/runtime/components/RuntimeCounters.vue";
import RuntimeDeadLetterPanel from "@/features/runtime/components/RuntimeDeadLetterPanel.vue";
import RuntimeDeadLetterRecoveryDialog from "@/features/runtime/components/RuntimeDeadLetterRecoveryDialog.vue";
import StorageReconciliationPanel from "@/features/runtime/components/StorageReconciliationPanel.vue";
import TerminalEventRepairDialog from "@/features/runtime/components/TerminalEventRepairDialog.vue";
const store = useRuntimeHealthStore();
function refreshAll() { store.load(); store.loadDeadLetters(); store.loadReconciliation(); }
onMounted(refreshAll);
</script>
<template>
  <div class="h-full overflow-auto">
    <header class="border-b border-[var(--color-border)] px-5 py-4">
      <div class="flex items-center justify-between gap-4">
        <div><div class="flex items-center gap-2"><Activity :size="18" class="text-[var(--color-muted)]" /><h1 class="font-medium text-[var(--color-text)]">Runtime Health</h1></div><p class="mt-1 text-xs text-[var(--color-muted)]">查看 Worker、消息积压、dead-letter 与 PostgreSQL 业务真源一致性。不会自动修复；符合条件的 DLQ 可经 L3 单次确认创建新 Run。</p></div>
        <button class="flex items-center gap-1.5 rounded border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-muted)] hover:bg-gray-50 disabled:opacity-50" :disabled="store.loading || store.deadLetterLoading || store.reconciliationLoading" @click="refreshAll"><RefreshCw :size="14" :class="{ 'animate-spin': store.loading || store.deadLetterLoading || store.reconciliationLoading }" />刷新</button>
      </div>
    </header>
    <main class="mx-auto max-w-7xl space-y-4 p-5">
      <div v-if="store.error" class="flex items-center gap-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600"><AlertCircle :size="14" />{{ store.error }}</div>
      <template v-if="store.health">
        <RuntimeSummary :health="store.health" />
        <div v-if="store.health.warnings.length" class="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700"><div v-for="warning in store.health.warnings" :key="warning">{{ warning }}</div></div>
        <RuntimeStreamTable :streams="store.health.streams" :dead-letters="store.health.dead_letters" />
        <RuntimeCounters :counters="store.health.counters" />
        <StorageReconciliationPanel :result="store.reconciliation" :loading="store.reconciliationLoading" :error="store.reconciliationError" @inspect-repair="store.inspectRepair" />
        <RuntimeDeadLetterPanel :records="store.deadLetters" :filters="store.deadLetterFilters" :loading="store.deadLetterLoading" :loading-more="store.deadLetterLoadingMore" :next-cursor="store.deadLetterNextCursor" :error="store.deadLetterError" @apply="store.loadDeadLetters" @load-more="store.loadMoreDeadLetters" @inspect-retry="store.inspectRecovery" />
        <p class="text-right text-[10px] text-[var(--color-muted)]">快照时间 {{ new Date(store.health.generated_at).toLocaleString() }}</p>
      </template>
      <div v-else-if="store.loading" class="rounded-lg border border-[var(--color-border)] bg-white p-8 text-center text-sm text-[var(--color-muted)]">正在读取运行时状态…</div>
    </main>
    <RuntimeDeadLetterRecoveryDialog
      :open="store.recoveryOpen"
      :record="store.recoveryRecord"
      :inspection="store.recoveryInspection"
      :request="store.recoveryRequest"
      :resolution="store.recoveryResolution"
      :loading="store.recoveryLoading"
      :error="store.recoveryError"
      @close="store.closeRecovery"
      @create-request="store.createRecoveryRequest"
      @resolve="store.resolveRecovery"
    />
    <TerminalEventRepairDialog
      :open="store.repairOpen"
      :issue="store.repairIssue"
      :inspection="store.repairInspection"
      :request="store.repairRequest"
      :resolution="store.repairResolution"
      :loading="store.repairLoading"
      :error="store.repairError"
      @close="store.closeRepair"
      @create-request="store.createRepairRequest"
      @resolve="store.resolveRepair"
    />
  </div>
</template>
