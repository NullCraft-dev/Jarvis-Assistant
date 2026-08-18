import { defineStore } from "pinia";
import { ref } from "vue";
import type {
  DlqRetryInspectionDTO,
  DlqRetryResolutionOutput,
  ListRuntimeDeadLettersInput,
  PermissionRequestDTO,
  RuntimeDeadLetterDTO,
  RuntimeHealthDTO,
  StorageReconciliationDTO,
  StorageReconciliationIssueDTO,
  TerminalEventRepairInspectionDTO,
  TerminalEventRepairResolutionOutput,
} from "@jarvis/shared";
import {
  createRuntimeDeadLetterRetryRequest,
  getRuntimeHealth,
  getStorageReconciliation,
  inspectTerminalEventRepair,
  createTerminalEventRepairRequest,
  resolveTerminalEventRepairRequest,
  inspectRuntimeDeadLetterRetry,
  listRuntimeDeadLetters,
  resolveRuntimeDeadLetterRetryRequest,
} from "@/api/client";

export const useRuntimeHealthStore = defineStore("runtimeHealth", () => {
  const health = ref<RuntimeHealthDTO | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);
  const reconciliation = ref<StorageReconciliationDTO | null>(null);
  const reconciliationLoading = ref(false);
  const reconciliationError = ref<string | null>(null);
  const repairOpen = ref(false);
  const repairIssue = ref<StorageReconciliationIssueDTO | null>(null);
  const repairInspection = ref<TerminalEventRepairInspectionDTO | null>(null);
  const repairRequest = ref<PermissionRequestDTO | null>(null);
  const repairResolution = ref<TerminalEventRepairResolutionOutput | null>(null);
  const repairLoading = ref(false);
  const repairError = ref<string | null>(null);
	const deadLetters = ref<RuntimeDeadLetterDTO[]>([]);
	const deadLetterFilters = ref<Omit<ListRuntimeDeadLettersInput, "before">>({ source: "run_queue", limit: 20 });
	const deadLetterNextCursor = ref<string | null>(null);
	const deadLetterLoading = ref(false);
	const deadLetterLoadingMore = ref(false);
	const deadLetterError = ref<string | null>(null);
	let deadLetterGeneration = 0;
  const recoveryOpen = ref(false);
  const recoveryRecord = ref<RuntimeDeadLetterDTO | null>(null);
  const recoveryInspection = ref<DlqRetryInspectionDTO | null>(null);
  const recoveryRequest = ref<PermissionRequestDTO | null>(null);
  const recoveryResolution = ref<DlqRetryResolutionOutput | null>(null);
  const recoveryLoading = ref(false);
  const recoveryError = ref<string | null>(null);
  async function load() {
    loading.value = true;
    error.value = null;
    try {
      const result = await getRuntimeHealth();
      if (!result.ok) { error.value = result.error.message || "加载运行时健康状态失败"; return; }
      health.value = result.data;
    } catch { error.value = "运行时诊断服务不可用"; }
    finally { loading.value = false; }
  }

  async function loadReconciliation() {
    reconciliationLoading.value = true;
    reconciliationError.value = null;
    try {
      const result = await getStorageReconciliation(50);
      if (!result.ok) {
        reconciliationError.value = result.error.message || "加载业务真源对账失败";
        return;
      }
      reconciliation.value = result.data;
    } catch {
      reconciliationError.value = "业务真源对账服务不可用";
    } finally {
      reconciliationLoading.value = false;
    }
  }

  async function inspectRepair(issue: StorageReconciliationIssueDTO) {
    if (issue.code !== "TERMINAL_EVENT_MISSING" || !issue.run_id) return;
    repairOpen.value = true;
    repairIssue.value = issue;
    repairInspection.value = null;
    repairRequest.value = null;
    repairResolution.value = null;
    repairError.value = null;
    repairLoading.value = true;
    try {
      const result = await inspectTerminalEventRepair(issue.run_id);
      if (!result.ok) { repairError.value = result.error.message || "检查修复资格失败"; return; }
      repairInspection.value = result.data;
    } catch { repairError.value = "受控修复服务不可用"; }
    finally { repairLoading.value = false; }
  }

  async function createRepairRequest() {
    if (!repairIssue.value?.run_id || !repairInspection.value?.eligible) return;
    repairLoading.value = true;
    repairError.value = null;
    try {
      const result = await createTerminalEventRepairRequest(repairIssue.value.run_id);
      if (!result.ok) { repairError.value = result.error.message || "创建确认请求失败"; return; }
      repairRequest.value = result.data.request;
    } catch { repairError.value = "受控修复服务不可用"; }
    finally { repairLoading.value = false; }
  }

  async function resolveRepair(decision: "allow_once" | "deny", note = "") {
    if (!repairRequest.value) return;
    repairLoading.value = true;
    repairError.value = null;
    try {
      const result = await resolveTerminalEventRepairRequest(
        repairRequest.value.id, decision, note,
      );
      if (!result.ok) { repairError.value = result.error.message || "提交修复决定失败"; return; }
      repairRequest.value = result.data.request;
      repairResolution.value = result.data;
      await loadReconciliation();
    } catch { repairError.value = "受控修复服务不可用"; }
    finally { repairLoading.value = false; }
  }

  function closeRepair() {
    if (!repairLoading.value) repairOpen.value = false;
  }

	async function loadDeadLetters(filters: Omit<ListRuntimeDeadLettersInput, "before"> = deadLetterFilters.value) {
		const generation = ++deadLetterGeneration;
		deadLetterFilters.value = { ...filters, limit: filters.limit ?? 20 };
		deadLetterLoading.value = true;
		deadLetterError.value = null;
		try {
			const result = await listRuntimeDeadLetters(deadLetterFilters.value);
			if (generation !== deadLetterGeneration) return;
			if (!result.ok) { deadLetterError.value = result.error.message || "加载 DLQ 记录失败"; return; }
			deadLetters.value = result.data.records;
			deadLetterNextCursor.value = result.data.next_cursor ?? null;
		} catch { if (generation === deadLetterGeneration) deadLetterError.value = "DLQ 诊断服务不可用"; }
		finally { if (generation === deadLetterGeneration) deadLetterLoading.value = false; }
	}

	async function loadMoreDeadLetters() {
		if (!deadLetterNextCursor.value || deadLetterLoadingMore.value) return;
		const generation = deadLetterGeneration;
		deadLetterLoadingMore.value = true;
		try {
			const result = await listRuntimeDeadLetters({ ...deadLetterFilters.value, before: deadLetterNextCursor.value });
			if (generation !== deadLetterGeneration) return;
			if (!result.ok) { deadLetterError.value = result.error.message || "加载更多 DLQ 记录失败"; return; }
			const known = new Set(deadLetters.value.map((record) => record.id));
			deadLetters.value.push(...result.data.records.filter((record) => !known.has(record.id)));
			deadLetterNextCursor.value = result.data.next_cursor ?? null;
		} catch { if (generation === deadLetterGeneration) deadLetterError.value = "DLQ 诊断服务不可用"; }
		finally { if (generation === deadLetterGeneration) deadLetterLoadingMore.value = false; }
	}

  async function inspectRecovery(record: RuntimeDeadLetterDTO) {
    recoveryOpen.value = true;
    recoveryRecord.value = record;
    recoveryInspection.value = null;
    recoveryRequest.value = null;
    recoveryResolution.value = null;
    recoveryError.value = null;
    recoveryLoading.value = true;
    try {
      const result = await inspectRuntimeDeadLetterRetry({ source: record.source, record_id: record.id });
      if (!result.ok) { recoveryError.value = result.error.message || "检查处置资格失败"; return; }
      recoveryInspection.value = result.data;
    } catch { recoveryError.value = "受控重试服务不可用"; }
    finally { recoveryLoading.value = false; }
  }

  async function createRecoveryRequest() {
    const record = recoveryRecord.value;
    if (!record || !recoveryInspection.value?.eligible) return;
    recoveryLoading.value = true;
    recoveryError.value = null;
    try {
      const result = await createRuntimeDeadLetterRetryRequest({ source: record.source, record_id: record.id });
      if (!result.ok) { recoveryError.value = result.error.message || "创建确认请求失败"; return; }
      recoveryRequest.value = result.data.request;
    } catch { recoveryError.value = "受控重试服务不可用"; }
    finally { recoveryLoading.value = false; }
  }

  async function resolveRecovery(decision: "allow_once" | "deny", note = "") {
    if (!recoveryRequest.value) return;
    recoveryLoading.value = true;
    recoveryError.value = null;
    try {
      const result = await resolveRuntimeDeadLetterRetryRequest(recoveryRequest.value.id, decision, note);
      if (!result.ok) { recoveryError.value = result.error.message || "提交处置决定失败"; return; }
      recoveryRequest.value = result.data.request;
      recoveryResolution.value = result.data;
      if (result.data.new_run) await load();
    } catch { recoveryError.value = "受控重试服务不可用"; }
    finally { recoveryLoading.value = false; }
  }

  function closeRecovery() {
    if (recoveryLoading.value) return;
    recoveryOpen.value = false;
  }

  return {
    health, loading, error, load,
    reconciliation, reconciliationLoading, reconciliationError, loadReconciliation,
    repairOpen, repairIssue, repairInspection, repairRequest, repairResolution,
    repairLoading, repairError, inspectRepair, createRepairRequest, resolveRepair, closeRepair,
    deadLetters, deadLetterFilters, deadLetterNextCursor, deadLetterLoading,
    deadLetterLoadingMore, deadLetterError, loadDeadLetters, loadMoreDeadLetters,
    recoveryOpen, recoveryRecord, recoveryInspection, recoveryRequest,
    recoveryResolution, recoveryLoading, recoveryError,
    inspectRecovery, createRecoveryRequest, resolveRecovery, closeRecovery,
  };
});
