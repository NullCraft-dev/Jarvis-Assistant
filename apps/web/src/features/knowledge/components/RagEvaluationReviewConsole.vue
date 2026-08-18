<script setup lang="ts">
import { ref, watch } from "vue";
import { Check, Eye, RefreshCw, ShieldCheck, Upload, X } from "@lucide/vue";
import type { ID, RagEvaluationReviewTargetDTO } from "@jarvis/shared";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useRagEvaluationReview } from "@/features/feedback/composables/useRagEvaluationReview";

const workspaceStore = useWorkspaceStore();
const props = defineProps<{ reviewTarget?: RagEvaluationReviewTargetDTO | null }>();
const emit = defineEmits<{ targetOpened: [] }>();
const review = useRagEvaluationReview();
const openingExternalTarget = ref(Boolean(props.reviewTarget));
const positiveIds = ref<ID[]>([]);
const negativeIds = ref<ID[]>([]);
const notes = ref("");

function toggle(values: ID[], id: ID) { return values.includes(id) ? values.filter((value) => value !== id) : [...values, id]; }
async function open(traceId: ID, workspaceId = workspaceStore.selectedWorkspaceId) {
  if (!workspaceId || !await review.inspect(workspaceId, traceId)) return false;
  positiveIds.value = [...(review.detail.value?.label?.positive_chunk_ids ?? [])];
  negativeIds.value = [...(review.detail.value?.label?.hard_negative_chunk_ids ?? [])];
  notes.value = review.detail.value?.label?.notes ?? "";
  return true;
}
async function privacy(decision: "approved" | "rejected") {
  if (!review.detail.value) return;
  await review.reviewPrivacy(review.detail.value.trace.workspace_id, review.detail.value.trace.trace_id, decision);
}
async function label(status: "draft" | "confirmed" | "rejected") {
  if (!review.detail.value) return;
  await review.saveLabel(review.detail.value.trace.workspace_id, review.detail.value.trace.trace_id, {
    status, positive_chunk_ids: positiveIds.value, hard_negative_chunk_ids: negativeIds.value, notes: notes.value,
  });
}
async function promote() {
  if (!review.detail.value) return;
  await review.promote(review.detail.value.trace.workspace_id, review.detail.value.trace.trace_id);
}
function closeDetail() {
  if (!review.mutating.value) review.detail.value = null;
}
watch([() => workspaceStore.selectedWorkspaceId, review.selectedPrivacy], ([workspaceId]) => review.load(workspaceId, openingExternalTarget.value), { immediate: true });
watch(() => props.reviewTarget, async (target) => {
  openingExternalTarget.value = Boolean(target);
  if (target && await open(target.trace_id, target.workspace_id)) emit("targetOpened");
}, { immediate: true });
</script>

<template>
  <section class="rounded-lg border bg-white p-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div class="flex items-center gap-2 text-sm font-medium"><ShieldCheck :size="15" />RAG 飞轮人工审核</div>
        <p class="mt-1 max-w-2xl text-xs text-[var(--color-muted)]">先做隐私复核，再确认检索金标，最后生成脱敏回归候选。候选不会自动进入正式 cohort，仍需通过版本化发布提交。</p>
      </div>
      <div class="flex items-center gap-2">
        <select v-model="review.selectedPrivacy.value" class="rounded border px-2 py-1.5 text-xs">
          <option value="pending">待隐私复核</option><option value="approved">已批准</option><option value="rejected">已拒绝</option><option value="all">全部</option>
        </select>
        <button class="flex items-center gap-1 rounded border px-2 py-1.5 text-xs" :disabled="review.loading.value" @click="review.load(workspaceStore.selectedWorkspaceId)"><RefreshCw :size="12" />刷新</button>
      </div>
    </div>
    <div class="mt-3 grid grid-cols-3 gap-2 text-center text-xs">
      <div class="rounded bg-slate-50 p-2"><p class="text-lg font-medium">{{ review.counts.value.pending }}</p><p class="text-[var(--color-muted)]">待隐私复核</p></div>
      <div class="rounded bg-blue-50 p-2"><p class="text-lg font-medium">{{ review.counts.value.confirmed }}</p><p class="text-[var(--color-muted)]">待晋升</p></div>
      <div class="rounded bg-emerald-50 p-2"><p class="text-lg font-medium">{{ review.counts.value.promoted }}</p><p class="text-[var(--color-muted)]">已生成候选</p></div>
    </div>
    <p v-if="review.error.value" class="mt-3 rounded bg-red-50 p-2 text-xs text-red-600">{{ review.error.value.message }}</p>
    <div v-if="review.loading.value && !review.detail.value" class="py-5 text-center text-xs text-[var(--color-muted)]">正在加载审核队列…</div>
    <div v-else-if="!review.items.value.length" class="mt-3 rounded border border-dashed p-5 text-center text-xs text-[var(--color-muted)]">当前筛选下没有轨迹</div>
    <div v-else class="mt-3 space-y-2">
      <article v-for="item in review.items.value" :key="item.trace_id" class="flex flex-wrap items-center justify-between gap-3 rounded border p-3 text-xs">
        <div>
          <p class="font-medium">Query {{ item.query_hash.slice(0, 12) }} <span v-if="item.context_truncated" class="ml-1 text-amber-600">上下文截断</span></p>
          <p class="mt-1 text-[var(--color-muted)]">隐私 {{ item.privacy_status }} · 标签 {{ item.label_status ?? "无" }} · 候选 {{ item.candidate_count }} / 上下文 {{ item.context_chunk_count }}</p>
        </div>
        <button class="flex items-center gap-1 rounded border px-2 py-1 text-blue-700" @click="open(item.trace_id)"><Eye :size="12" />审核</button>
      </article>
    </div>

  </section>

  <Teleport to="body">
    <div
      v-if="review.detail.value"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-3 sm:p-5"
      @click.self="closeDetail"
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="rag-evaluation-review-title"
        class="flex max-h-[calc(100vh-1.5rem)] w-full max-w-4xl flex-col overflow-hidden rounded-xl bg-white text-xs shadow-xl sm:max-h-[calc(100vh-2.5rem)]"
      >
        <header class="flex shrink-0 items-start justify-between gap-3 border-b px-4 py-3 sm:px-5 sm:py-4">
          <div>
            <h3 id="rag-evaluation-review-title" class="text-sm font-medium">审核轨迹 {{ review.detail.value.trace.trace_id.slice(0, 8) }}</h3>
            <p class="mt-1 text-[var(--color-muted)]">隐私 {{ review.detail.value.trace.privacy_status }} · 标签 {{ review.detail.value.label?.status ?? "无" }}</p>
          </div>
          <button class="rounded p-1.5 text-[var(--color-muted)] hover:bg-gray-100 disabled:opacity-40" :disabled="review.mutating.value" aria-label="关闭审核弹窗" @click="closeDetail"><X :size="16" /></button>
        </header>

        <div class="min-h-0 flex-1 overflow-auto bg-blue-50/30 p-4 sm:p-5">
          <template v-if="review.detail.value.trace.privacy_status === 'pending'">
            <div class="rounded border border-amber-200 bg-amber-50 p-3 leading-5 text-amber-800">查询与证据正文已隐藏。批准前请确认该轨迹可用于离线评测；拒绝后不会进入标签和晋升流程。</div>
            <div class="mt-4 flex flex-wrap justify-end gap-2"><button class="rounded border bg-white px-3 py-2" :disabled="review.mutating.value" @click="privacy('rejected')">拒绝</button><button class="flex items-center gap-1 rounded bg-blue-600 px-3 py-2 text-white" :disabled="review.mutating.value" @click="privacy('approved')"><Check :size="12" />批准隐私</button></div>
          </template>
          <div v-else-if="review.detail.value.trace.privacy_status === 'rejected'" class="rounded bg-slate-100 p-3 text-[var(--color-muted)]">该轨迹已拒绝，正文保持隐藏且不能进入晋升链路。</div>
          <template v-else>
            <div class="rounded border bg-white p-3 leading-5"><span class="font-medium">用户问题：</span>{{ review.detail.value.query }}</div>
            <div class="mt-4 space-y-2">
              <p class="font-medium">证据标注</p>
              <article v-for="evidence in review.detail.value.evidence" :key="evidence.chunk_id" class="rounded border bg-white p-3">
                <div class="flex flex-wrap items-center justify-between gap-2">
                  <p>候选 #{{ evidence.candidate_rank ?? '-' }} · 重排 #{{ evidence.reranked_rank ?? '-' }} · {{ evidence.in_context ? '已进上下文' : '未进上下文' }}</p>
                  <div v-if="review.detail.value.label?.status !== 'promoted'" class="flex gap-1">
                    <button class="rounded border px-2 py-1" :class="positiveIds.includes(evidence.chunk_id) ? 'border-emerald-500 bg-emerald-50 text-emerald-700' : ''" @click="positiveIds = toggle(positiveIds, evidence.chunk_id); negativeIds = negativeIds.filter((id) => id !== evidence.chunk_id)">正例</button>
                    <button class="rounded border px-2 py-1" :class="negativeIds.includes(evidence.chunk_id) ? 'border-amber-500 bg-amber-50 text-amber-700' : ''" @click="negativeIds = toggle(negativeIds, evidence.chunk_id); positiveIds = positiveIds.filter((id) => id !== evidence.chunk_id)">难负例</button>
                  </div>
                </div>
                <p class="mt-2 break-words leading-5 text-[var(--color-muted)]">{{ evidence.snippet }}</p>
              </article>
            </div>
            <textarea v-if="review.detail.value.label?.status !== 'promoted'" v-model="notes" maxlength="500" class="mt-3 min-h-20 w-full rounded border bg-white p-2" placeholder="审核说明（可选）"></textarea>
            <div v-if="review.detail.value.label?.status === 'promoted' && review.detail.value.promotion_candidate" class="mt-3 rounded border border-emerald-200 bg-emerald-50 p-3 text-emerald-800">
              <p class="font-medium">脱敏回归候选已生成</p><p class="mt-1 break-all font-mono">{{ review.detail.value.promotion_candidate.query_hash }}</p><p class="mt-1">不含原始问题和正文；等待 release commit 纳入正式 cohort。</p>
            </div>
            <div v-else class="mt-4 flex flex-wrap justify-end gap-2 border-t pt-4">
              <button class="rounded border bg-white px-3 py-2" :disabled="review.mutating.value || !positiveIds.length" @click="label('rejected')">驳回标签</button>
              <button class="rounded border bg-white px-3 py-2" :disabled="review.mutating.value || !positiveIds.length" @click="label('draft')">保存草稿</button>
              <button class="rounded bg-blue-600 px-3 py-2 text-white" :disabled="review.mutating.value || !positiveIds.length" @click="label('confirmed')">确认金标</button>
              <button v-if="review.detail.value.label?.status === 'confirmed'" class="flex items-center gap-1 rounded bg-emerald-600 px-3 py-2 text-white" :disabled="review.mutating.value" @click="promote"><Upload :size="12" />生成回归候选</button>
            </div>
          </template>
        </div>
      </section>
    </div>
  </Teleport>
</template>
