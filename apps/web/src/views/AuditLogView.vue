<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import type { AuditLogDTO, ListAuditLogsInput } from "@jarvis/shared";
import { ScrollText, AlertCircle } from "@lucide/vue";
import { useAuditLogStore } from "@/stores/auditLogStore";
import AuditFilters from "@/features/audit/components/AuditFilters.vue";
import AuditLogList from "@/features/audit/components/AuditLogList.vue";
import AuditLogDetail from "@/features/audit/components/AuditLogDetail.vue";

const auditStore = useAuditLogStore();
const selectedId = ref<string | null>(null);
const selectedLog = computed(() => auditStore.auditLogs.find((log) => log.id === selectedId.value) ?? null);
function apply(filters: Omit<ListAuditLogsInput, "before">) { selectedId.value = null; auditStore.load(filters); }
function select(log: AuditLogDTO) { selectedId.value = log.id; }
onMounted(() => auditStore.load());
</script>

<template>
  <div class="h-full overflow-auto"><header class="border-b border-[var(--color-border)] px-5 py-4"><div class="flex items-center gap-2"><ScrollText :size="18" class="text-[var(--color-muted)]" /><h1 class="font-medium text-[var(--color-text)]">审计查询</h1></div><p class="mt-1 text-xs text-[var(--color-muted)]">只读查看已持久化的权限、工具、工作区与模型操作记录。敏感内容不会显示。</p></header>
    <main class="mx-auto max-w-7xl space-y-4 p-5"><AuditFilters :loading="auditStore.loading" :initial="auditStore.filters" @apply="apply" /><div v-if="auditStore.error" class="flex items-center gap-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600"><AlertCircle :size="14" />{{ auditStore.error }}</div><div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]"><div class="space-y-3"><AuditLogList :logs="auditStore.auditLogs" :selected-id="selectedId" @select="select" /><button v-if="auditStore.nextCursor" class="w-full rounded border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-muted)] hover:bg-gray-50 disabled:opacity-50" :disabled="auditStore.loadingMore" @click="auditStore.loadMore">{{ auditStore.loadingMore ? "加载中…" : "加载更多" }}</button></div><AuditLogDetail :log="selectedLog" /></div></main>
  </div>
</template>
