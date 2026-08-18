<script setup lang="ts">
import { computed, ref } from "vue";
import { AlertTriangle, ThumbsDown, ThumbsUp } from "@lucide/vue";
import type { ID, RagFeedbackKind } from "@jarvis/shared";
import { useMessageFeedback } from "@/features/feedback/composables/useMessageFeedback";

const props = defineProps<{ messageId: ID; content: string }>();
const { submitting, submittedKind, error, submit } = useMessageFeedback(props.messageId);
const showCitationChoices = ref(false);
const citationChunkIds = computed(() => {
  const matches = props.content.matchAll(/chunk:([0-9a-f]{8}-[0-9a-f-]{27,36})/gi);
  return [...new Set(Array.from(matches, (match) => match[1]!.toLowerCase()))];
});

const labels: Record<RagFeedbackKind, string> = {
  helpful: "有帮助",
  unhelpful: "没帮助",
  citation_incorrect: "引用有误",
  evidence_insufficient: "依据不足",
};
</script>

<template>
  <div class="mt-2 border-t border-[var(--color-border)] pt-1.5 text-[11px]">
    <p v-if="submittedKind" class="text-emerald-700">
      已记录“{{ labels[submittedKind] }}”，将进入审核队列
    </p>
    <div v-else class="flex flex-wrap items-center gap-1">
      <span class="mr-1 text-[var(--color-muted)]">这次回答：</span>
      <button class="feedback-button" :disabled="submitting" @click="submit('helpful')">
        <ThumbsUp :size="12" />有帮助
      </button>
      <button class="feedback-button" :disabled="submitting" @click="submit('unhelpful')">
        <ThumbsDown :size="12" />没帮助
      </button>
      <button class="feedback-button" :disabled="submitting" @click="submit('evidence_insufficient')">
        <AlertTriangle :size="12" />依据不足
      </button>
      <button
        v-if="citationChunkIds.length"
        class="feedback-button"
        :disabled="submitting"
        @click="showCitationChoices = !showCitationChoices"
      >引用有误</button>
    </div>
    <div v-if="showCitationChoices && !submittedKind" class="mt-1.5 rounded bg-amber-50 p-2 text-amber-900">
      <p class="mb-1">请选择有问题的引用：</p>
      <button
        v-for="chunkId in citationChunkIds"
        :key="chunkId"
        class="mr-1 mt-1 rounded border border-amber-300 bg-white px-1.5 py-1 font-mono"
        :disabled="submitting"
        @click="submit('citation_incorrect', chunkId)"
      >{{ chunkId.slice(0, 8) }}</button>
    </div>
    <p v-if="error" class="mt-1 text-red-600">{{ error.message }}</p>
  </div>
</template>

<style scoped>
.feedback-button {
  display: inline-flex;
  align-items: center;
  gap: 0.2rem;
  border-radius: 0.25rem;
  padding: 0.2rem 0.35rem;
  color: var(--color-muted);
}
.feedback-button:hover { background: #f3f4f6; color: var(--color-text); }
.feedback-button:disabled { cursor: wait; opacity: 0.5; }
</style>
