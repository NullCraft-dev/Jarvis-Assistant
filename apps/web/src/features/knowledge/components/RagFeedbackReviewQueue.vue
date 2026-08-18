<script setup lang="ts">
import { ref, watch } from "vue";
import { MessageSquareWarning, RefreshCw, Search, X } from "@lucide/vue";
import type { ID, RagFeedbackFailureCategory, RagFeedbackKind } from "@jarvis/shared";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { useRagFeedbackQueue } from "@/features/feedback/composables/useRagFeedbackQueue";

const workspaceStore = useWorkspaceStore();
const queue = useRagFeedbackQueue();
const kindLabels: Record<RagFeedbackKind, string> = {
  helpful: "有帮助",
  unhelpful: "没帮助",
  citation_incorrect: "引用有误",
  evidence_insufficient: "依据不足",
};
const categoryLabels: Record<RagFeedbackFailureCategory, string> = {
  candidate_miss: "候选召回遗漏", reranker_miss: "重排失误", context_omission: "上下文遗漏",
  context_truncated: "上下文截断", citation_mismatch: "引用不匹配", answer_generation: "回答生成问题",
  insufficient_evidence: "知识库证据不足", other: "其他",
};
const category = ref<RagFeedbackFailureCategory>("other");
const positiveIds = ref<ID[]>([]);
const negativeIds = ref<ID[]>([]);

async function openDetail(id: ID) {
  if (await queue.inspect(id)) {
    category.value = queue.detail.value?.feedback.failure_category ?? "other";
    positiveIds.value = queue.detail.value?.label?.source === "user_feedback" ? [...queue.detail.value.label.positive_chunk_ids] : [];
    negativeIds.value = queue.detail.value?.label?.source === "user_feedback" ? [...queue.detail.value.label.hard_negative_chunk_ids] : [];
  }
}
function toggle(values: ID[], id: ID) { return values.includes(id) ? values.filter((value) => value !== id) : [...values, id]; }
async function saveTriage() {
  const detail = queue.detail.value; if (!detail) return;
  await queue.triage(workspaceStore.selectedWorkspaceId, detail.feedback.id, {
    failure_category: category.value, positive_chunk_ids: positiveIds.value, hard_negative_chunk_ids: negativeIds.value,
  });
}

watch(
  [() => workspaceStore.selectedWorkspaceId, queue.selectedStatus],
  ([workspaceId]) => queue.load(workspaceId),
  { immediate: true },
);
</script>

<template>
  <section class="rounded-lg border bg-white p-4">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div>
        <div class="flex items-center gap-2 text-sm font-medium"><MessageSquareWarning :size="15" />RAG 反馈审核</div>
        <p class="mt-1 text-xs text-[var(--color-muted)]">用户反馈只形成候选；确认金标与晋升仍需开发者复核。</p>
      </div>
      <div class="flex items-center gap-2">
        <select v-model="queue.selectedStatus.value" class="rounded border px-2 py-1.5 text-xs">
          <option value="pending">待审核</option><option value="reviewed">已查看</option><option value="dismissed">已忽略</option>
        </select>
        <button class="flex items-center gap-1 rounded border px-2 py-1.5 text-xs" :disabled="queue.loading.value" @click="queue.load(workspaceStore.selectedWorkspaceId)"><RefreshCw :size="12" />刷新</button>
      </div>
    </div>
    <p v-if="queue.error.value" class="mt-3 rounded bg-red-50 p-2 text-xs text-red-600">{{ queue.error.value.message }}</p>
    <div v-if="queue.loading.value" class="py-5 text-center text-xs text-[var(--color-muted)]">正在加载反馈…</div>
    <div v-else-if="!queue.items.value.length" class="mt-3 rounded border border-dashed p-5 text-center text-xs text-[var(--color-muted)]">当前队列为空</div>
    <div v-else class="mt-3 space-y-2">
      <article v-for="item in queue.items.value" :key="item.id" class="rounded border p-3 text-xs">
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <p class="font-medium">{{ kindLabels[item.kind] }} <span v-if="item.context_truncated" class="ml-1 text-amber-600">上下文曾截断</span></p>
            <p class="mt-1 text-[var(--color-muted)]">Trace {{ item.trace_id.slice(0, 8) }} · Query {{ item.query_hash?.slice(0, 10) }} · 返回 {{ item.result_count ?? 0 }} 条</p>
            <p v-if="item.citation_chunk_id" class="mt-1 font-mono text-[var(--color-muted)]">引用 {{ item.citation_chunk_id }}</p>
          </div>
          <div v-if="item.status === 'pending'" class="flex gap-1">
            <button class="flex items-center gap-1 rounded border px-2 py-1 text-blue-700" :disabled="queue.inspectingId.value === item.id" @click="openDetail(item.id)"><Search :size="12" />诊断</button>
            <button class="flex items-center gap-1 rounded border px-2 py-1 text-[var(--color-muted)]" :disabled="queue.resolvingId.value === item.id" @click="queue.resolve(workspaceStore.selectedWorkspaceId, item.id, 'dismissed')"><X :size="12" />忽略</button>
          </div>
        </div>
      </article>
    </div>
    <div v-if="queue.detail.value" class="mt-4 rounded-lg border border-blue-200 bg-blue-50/30 p-4 text-xs">
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="font-medium">检索链路诊断 · {{ kindLabels[queue.detail.value.feedback.kind] }}</p>
          <p class="mt-1 text-[var(--color-muted)]">隐私状态：{{ queue.detail.value.privacy_status }} · 标签：{{ queue.detail.value.label?.status ?? "无" }}</p>
        </div>
        <button class="rounded p-1 text-[var(--color-muted)]" @click="queue.detail.value = null"><X :size="14" /></button>
      </div>
      <div v-if="queue.detail.value.query" class="mt-3 rounded bg-white p-3"><span class="font-medium">用户问题：</span>{{ queue.detail.value.query }}</div>
      <div v-else class="mt-3 rounded bg-amber-50 p-3 text-amber-700">查询与证据正文尚未展示：需要先在飞轮审核中通过该 trace 的隐私复核。</div>
      <label class="mt-3 block font-medium">失败原因
        <select v-model="category" class="mt-1 w-full rounded border bg-white px-2 py-2">
          <option v-for="(label, value) in categoryLabels" :key="value" :value="value">{{ label }}</option>
        </select>
      </label>
      <div class="mt-3 space-y-2">
        <p class="font-medium">阶段证据 <span class="font-normal text-[var(--color-muted)]">（正例/难负例只会形成 draft）</span></p>
        <article v-for="evidence in queue.detail.value.evidence" :key="evidence.chunk_id" class="rounded border bg-white p-3">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <p>候选 #{{ evidence.candidate_rank ?? "-" }} · 重排 #{{ evidence.reranked_rank ?? "-" }} · {{ evidence.in_context ? "已进上下文" : "未进上下文" }}</p>
            <div v-if="queue.detail.value.privacy_status === 'approved' && (!queue.detail.value.label || (queue.detail.value.label.status === 'draft' && queue.detail.value.label.source === 'user_feedback'))" class="flex gap-1">
              <button class="rounded border px-2 py-1" :class="positiveIds.includes(evidence.chunk_id) ? 'border-emerald-500 bg-emerald-50 text-emerald-700' : ''" @click="positiveIds = toggle(positiveIds, evidence.chunk_id); negativeIds = negativeIds.filter((id) => id !== evidence.chunk_id)">正例</button>
              <button class="rounded border px-2 py-1" :class="negativeIds.includes(evidence.chunk_id) ? 'border-amber-500 bg-amber-50 text-amber-700' : ''" @click="negativeIds = toggle(negativeIds, evidence.chunk_id); positiveIds = positiveIds.filter((id) => id !== evidence.chunk_id)">难负例</button>
            </div>
          </div>
          <p v-if="evidence.snippet" class="mt-2 text-[var(--color-muted)]">{{ evidence.snippet }}</p>
          <p class="mt-2 font-mono text-[10px] text-[var(--color-muted)]">{{ evidence.chunk_id }} · {{ evidence.content_hash.slice(0, 10) }}</p>
        </article>
      </div>
      <div class="mt-3 flex justify-end">
        <button class="rounded bg-[var(--color-primary)] px-3 py-2 text-white disabled:opacity-50" :disabled="queue.resolvingId.value === queue.detail.value.feedback.id || negativeIds.length > 0 && positiveIds.length === 0" @click="saveTriage">保存诊断{{ positiveIds.length ? "并生成草稿标签" : "" }}</button>
      </div>
    </div>
  </section>
</template>
