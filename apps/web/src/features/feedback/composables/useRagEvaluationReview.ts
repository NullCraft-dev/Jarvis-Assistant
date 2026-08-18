import { computed, ref } from "vue";
import type { AppError, ID, RagEvaluationPrivacyStatus, RagEvaluationTraceDetailOutput, RagEvaluationTraceDTO, ReviewRagTraceLabelInput } from "@jarvis/shared";
import { inspectRagEvaluationTrace, listRagEvaluationTraces, promoteRagEvaluationTrace, reviewRagEvaluationLabel, reviewRagEvaluationPrivacy } from "@/api/client";

export function useRagEvaluationReview() {
  const items = ref<RagEvaluationTraceDTO[]>([]);
  const detail = ref<RagEvaluationTraceDetailOutput | null>(null);
  const selectedPrivacy = ref<RagEvaluationPrivacyStatus | "all">("pending");
  const loading = ref(false);
  const mutating = ref(false);
  const error = ref<AppError | null>(null);
  const counts = computed(() => ({
    pending: items.value.filter((item) => item.privacy_status === "pending").length,
    confirmed: items.value.filter((item) => item.label_status === "confirmed").length,
    promoted: items.value.filter((item) => item.label_status === "promoted").length,
  }));

  async function load(workspaceId?: ID | null, preserveDetail = false) {
    if (!workspaceId) { items.value = []; detail.value = null; return; }
    loading.value = true; error.value = null;
    const result = await listRagEvaluationTraces(workspaceId, selectedPrivacy.value);
    loading.value = false;
    if (!result.ok) { error.value = result.error; return; }
    items.value = result.data.traces;
    if (!preserveDetail && detail.value && !items.value.some((item) => item.trace_id === detail.value?.trace.trace_id)) detail.value = null;
  }
  async function inspect(workspaceId: ID, traceId: ID) {
    loading.value = true; error.value = null;
    const result = await inspectRagEvaluationTrace(workspaceId, traceId);
    loading.value = false;
    if (!result.ok) { error.value = result.error; return false; }
    detail.value = result.data; return true;
  }
  async function mutate(workspaceId: ID, action: () => ReturnType<typeof inspectRagEvaluationTrace>) {
    mutating.value = true; error.value = null;
    const result = await action();
    mutating.value = false;
    if (!result.ok) { error.value = result.error; return false; }
    detail.value = result.data;
    await load(workspaceId, true);
    return true;
  }
  const reviewPrivacy = (workspaceId: ID, traceId: ID, decision: "approved" | "rejected") => mutate(workspaceId, () => reviewRagEvaluationPrivacy(workspaceId, traceId, decision));
  const saveLabel = (workspaceId: ID, traceId: ID, input: Omit<ReviewRagTraceLabelInput, "workspace_id">) => mutate(workspaceId, () => reviewRagEvaluationLabel(traceId, { ...input, workspace_id: workspaceId }));
  const promote = (workspaceId: ID, traceId: ID) => mutate(workspaceId, () => promoteRagEvaluationTrace(workspaceId, traceId));
  return { items, detail, selectedPrivacy, loading, mutating, error, counts, load, inspect, reviewPrivacy, saveLabel, promote };
}
