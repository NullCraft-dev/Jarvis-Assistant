import { ref } from "vue";
import type { AppError, ID, RagFeedbackDetailOutput, RagFeedbackDTO, RagFeedbackStatus, TriageRagFeedbackInput } from "@jarvis/shared";
import * as api from "@/api/client";

export function useRagFeedbackQueue() {
  const items = ref<RagFeedbackDTO[]>([]);
  const loading = ref(false);
  const resolvingId = ref<ID | null>(null);
  const error = ref<AppError | null>(null);
  const selectedStatus = ref<RagFeedbackStatus>("pending");
  const detail = ref<RagFeedbackDetailOutput | null>(null);
  const inspectingId = ref<ID | null>(null);

  async function load(workspaceId: ID | null) {
    if (!workspaceId) { items.value = []; return; }
    loading.value = true;
    error.value = null;
    try {
      const result = await api.listRagFeedback(workspaceId, selectedStatus.value, 50);
      if (!result.ok) { error.value = result.error; items.value = []; return; }
      items.value = result.data.feedback;
    } catch {
      error.value = { code: "RAG_FEEDBACK_UNAVAILABLE", message: "反馈队列暂不可用", category: "runtime", recoverable: true };
    } finally { loading.value = false; }
  }

  async function inspect(feedbackId: ID) {
    inspectingId.value = feedbackId; error.value = null;
    try {
      const result = await api.inspectRagFeedback(feedbackId);
      if (!result.ok) { error.value = result.error; return false; }
      detail.value = result.data; return true;
    } catch {
      error.value = { code: "RAG_FEEDBACK_INSPECT_FAILED", message: "反馈诊断详情读取失败", category: "runtime", recoverable: true }; return false;
    } finally { inspectingId.value = null; }
  }

  async function triage(workspaceId: ID | null, feedbackId: ID, input: TriageRagFeedbackInput) {
    resolvingId.value = feedbackId; error.value = null;
    try {
      const result = await api.triageRagFeedback(feedbackId, input);
      if (!result.ok) { error.value = result.error; return false; }
      detail.value = null; await load(workspaceId); return true;
    } catch {
      error.value = { code: "RAG_FEEDBACK_TRIAGE_FAILED", message: "反馈诊断保存失败", category: "runtime", recoverable: true }; return false;
    } finally { resolvingId.value = null; }
  }

  async function resolve(workspaceId: ID | null, feedbackId: ID, status: "reviewed" | "dismissed") {
    resolvingId.value = feedbackId;
    error.value = null;
    try {
      const result = await api.resolveRagFeedback(feedbackId, status);
      if (!result.ok) { error.value = result.error; return false; }
      await load(workspaceId);
      return true;
    } catch {
      error.value = { code: "RAG_FEEDBACK_RESOLVE_FAILED", message: "反馈处理失败", category: "runtime", recoverable: true };
      return false;
    } finally { resolvingId.value = null; }
  }

  return { items, loading, resolvingId, error, selectedStatus, detail, inspectingId, load, inspect, triage, resolve };
}
