<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ClipboardList, MessageSquareWarning, ShieldCheck, TestTube2 } from "@lucide/vue";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import RagFeedbackReviewQueue from "@/features/knowledge/components/RagFeedbackReviewQueue.vue";
import RagEvaluationReviewConsole from "@/features/knowledge/components/RagEvaluationReviewConsole.vue";
import RagQualityGateHistory from "@/features/knowledge/components/RagQualityGateHistory.vue";
import RagQualityIssueLedger from "@/features/knowledge/components/RagQualityIssueLedger.vue";
import type { RagEvaluationReviewTargetDTO } from "@jarvis/shared";

const workspaces = useWorkspaceStore();
const activeTab = ref<"feedback" | "evaluation" | "gates" | "issues">("feedback");
const reviewTarget = ref<RagEvaluationReviewTargetDTO | null>(null);
function openReviewTarget(target: RagEvaluationReviewTargetDTO) { reviewTarget.value = target; activeTab.value = "evaluation"; }

onMounted(() => workspaces.loadWorkspaces());
</script>

<template>
  <main class="min-h-0 flex-1 overflow-auto">
    <div class="mx-auto max-w-6xl space-y-5 p-4 sm:p-5">
      <section>
        <h2 class="text-lg font-medium">RAG 质量中心</h2>
        <p class="mt-1 text-xs leading-5 text-[var(--color-muted)]">这里是人工质量审核工作区，不改变知识文档或 RAG 文档的生命周期。</p>
      </section>

      <div class="inline-flex max-w-full gap-1 overflow-x-auto rounded-lg bg-gray-100 p-1" role="tablist" aria-label="RAG 质量审核类型">
        <button
          class="flex shrink-0 items-center gap-1.5 rounded-md px-3 py-2 text-xs transition"
          :class="activeTab === 'feedback' ? 'bg-white font-medium text-blue-700 shadow-sm' : 'text-[var(--color-muted)]'"
          role="tab"
          :aria-selected="activeTab === 'feedback'"
          @click="activeTab = 'feedback'"
        >
          <MessageSquareWarning :size="14" />用户反馈
        </button>
        <button
          class="flex shrink-0 items-center gap-1.5 rounded-md px-3 py-2 text-xs transition"
          :class="activeTab === 'evaluation' ? 'bg-white font-medium text-emerald-700 shadow-sm' : 'text-[var(--color-muted)]'"
          role="tab"
          :aria-selected="activeTab === 'evaluation'"
          @click="activeTab = 'evaluation'"
        >
          <ShieldCheck :size="14" />评测与飞轮
        </button>
        <button
          class="flex shrink-0 items-center gap-1.5 rounded-md px-3 py-2 text-xs transition"
          :class="activeTab === 'gates' ? 'bg-white font-medium text-violet-700 shadow-sm' : 'text-[var(--color-muted)]'"
          role="tab"
          :aria-selected="activeTab === 'gates'"
          @click="activeTab = 'gates'"
        >
          <TestTube2 :size="14" />发布门禁
        </button>
        <button
          class="flex shrink-0 items-center gap-1.5 rounded-md px-3 py-2 text-xs transition"
          :class="activeTab === 'issues' ? 'bg-white font-medium text-amber-700 shadow-sm' : 'text-[var(--color-muted)]'"
          role="tab"
          :aria-selected="activeTab === 'issues'"
          @click="activeTab = 'issues'"
        >
          <ClipboardList :size="14" />问题台账
        </button>
      </div>

      <RagFeedbackReviewQueue v-if="activeTab === 'feedback'" />
      <RagEvaluationReviewConsole v-else-if="activeTab === 'evaluation'" :review-target="reviewTarget" @target-opened="reviewTarget = null" />
      <RagQualityGateHistory v-else-if="activeTab === 'gates'" @review-target="openReviewTarget" />
      <RagQualityIssueLedger v-else @review-target="openReviewTarget" />
    </div>
  </main>
</template>
