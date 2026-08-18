// Audit Log Store — 只消费 Gateway 提供的安全审计 DTO；不推断或修改审计真相。

import { defineStore } from "pinia";
import { ref } from "vue";
import type { AuditLogDTO, ListAuditLogsInput } from "@jarvis/shared";
import { listAuditLogs } from "@/api/client";

export const useAuditLogStore = defineStore("auditLog", () => {
  const auditLogs = ref<AuditLogDTO[]>([]);
  const nextCursor = ref<string | null>(null);
  const loading = ref(false);
  const loadingMore = ref(false);
  const error = ref<string | null>(null);
  const filters = ref<Omit<ListAuditLogsInput, "before">>({ limit: 50 });
  let generation = 0;

  async function load(nextFilters: Omit<ListAuditLogsInput, "before"> = filters.value) {
    const requestGeneration = ++generation;
    filters.value = { ...nextFilters, limit: nextFilters.limit ?? 50 };
    loading.value = true;
    error.value = null;
    try {
      const result = await listAuditLogs(filters.value);
      if (requestGeneration !== generation) return;
      if (!result.ok) { error.value = result.error.message || "加载审计日志失败"; return; }
      auditLogs.value = result.data.audit_logs;
      nextCursor.value = result.data.next_cursor ?? null;
    } catch {
      if (requestGeneration === generation) error.value = "审计查询服务不可用";
    } finally {
      if (requestGeneration === generation) loading.value = false;
    }
  }

  async function loadMore() {
    if (!nextCursor.value || loadingMore.value) return;
    const requestGeneration = generation;
    loadingMore.value = true;
    try {
      const result = await listAuditLogs({ ...filters.value, before: nextCursor.value });
      if (requestGeneration !== generation) return;
      if (!result.ok) { error.value = result.error.message || "加载更多审计日志失败"; return; }
      const known = new Set(auditLogs.value.map((log) => log.id));
      auditLogs.value.push(...result.data.audit_logs.filter((log) => !known.has(log.id)));
      nextCursor.value = result.data.next_cursor ?? null;
    } catch {
      if (requestGeneration === generation) error.value = "审计查询服务不可用";
    } finally {
      if (requestGeneration === generation) loadingMore.value = false;
    }
  }

  return { auditLogs, nextCursor, loading, loadingMore, error, filters, load, loadMore };
});
