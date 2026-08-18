import { ref } from "vue";
import type { AppError, ID, RagFeedbackKind } from "@jarvis/shared";
import * as api from "@/api/client";

export function useMessageFeedback(messageId: ID) {
  const submitting = ref(false);
  const submittedKind = ref<RagFeedbackKind | null>(null);
  const error = ref<AppError | null>(null);

  async function submit(kind: RagFeedbackKind, citationChunkId?: ID) {
    submitting.value = true;
    error.value = null;
    try {
      const result = await api.submitRagFeedback({
        message_id: messageId,
        kind,
        citation_chunk_id: citationChunkId,
      });
      if (!result.ok) {
        error.value = result.error;
        return false;
      }
      submittedKind.value = result.data.feedback.kind;
      return true;
    } catch {
      error.value = {
        code: "RAG_FEEDBACK_UNAVAILABLE",
        message: "反馈暂时无法提交，请稍后重试",
        category: "runtime",
        recoverable: true,
      };
      return false;
    } finally {
      submitting.value = false;
    }
  }

  return { submitting, submittedKind, error, submit };
}
