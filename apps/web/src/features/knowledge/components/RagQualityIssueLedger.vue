<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ClipboardList, Eye, RefreshCw } from "@lucide/vue";
import type {
  AppError, RagEvaluationReviewTargetDTO, RagQualityIssueDTO,
  RagQualityIssueLedgerItemDTO, RagQualityIssueSummaryDTO,
} from "@jarvis/shared";
import { listRagQualityIssues, updateRagQualityIssue } from "@/api/client";

const emit = defineEmits<{ reviewTarget: [target: RagEvaluationReviewTargetDTO] }>();
const issues = ref<RagQualityIssueLedgerItemDTO[]>([]);
const summary = ref<RagQualityIssueSummaryDTO>({ total: 0, open: 0, in_progress: 0, resolved: 0, verified: 0, dismissed: 0 });
const status = ref<RagQualityIssueDTO["status"] | "all">("all");
const owner = ref<RagQualityIssueDTO["owner"] | "all">("all");
const failureType = ref("all");
const notes = ref<Record<string, string>>({});
const loading = ref(false);
const mutatingId = ref<string | null>(null);
const error = ref<AppError | null>(null);

const statusOptions: Array<{ value: RagQualityIssueDTO["status"] | "all"; label: string }> = [
  { value: "all", label: "全部状态" }, { value: "open", label: "待处理" },
  { value: "in_progress", label: "处理中" }, { value: "resolved", label: "等待回归验证" },
  { value: "verified", label: "已验证" }, { value: "dismissed", label: "已忽略" },
];
const ownerOptions: Array<{ value: RagQualityIssueDTO["owner"] | "all"; label: string }> = [
  { value: "all", label: "全部负责人" }, { value: "data_quality", label: "数据与金标" },
  { value: "candidate_recall", label: "候选召回" }, { value: "reranker", label: "重排" },
  { value: "context_assembly", label: "上下文组装" },
];

async function load() {
  loading.value = true; error.value = null;
  const result = await listRagQualityIssues(status.value, owner.value, failureType.value, 50);
  loading.value = false;
  if (!result.ok) { error.value = result.error; return; }
  issues.value = result.data.issues;
  summary.value = result.data.summary;
  notes.value = Object.fromEntries(result.data.issues.map((item) => [item.issue.id, item.issue.resolution_note]));
}
async function mutate(item: RagQualityIssueLedgerItemDTO, nextStatus: "open" | "in_progress" | "resolved" | "dismissed", nextOwner = item.issue.owner) {
  mutatingId.value = item.issue.id; error.value = null;
  const result = await updateRagQualityIssue(item.issue.id, {
    expected_version: item.issue.version, owner: nextOwner, status: nextStatus,
    resolution_note: notes.value[item.issue.id] ?? item.issue.resolution_note,
  });
  mutatingId.value = null;
  if (!result.ok) { error.value = result.error; return; }
  item.issue = result.data.issue;
  await load();
}
function changeOwner(item: RagQualityIssueLedgerItemDTO, event: Event) {
  mutate(item, item.issue.status as "open" | "in_progress", (event.target as HTMLSelectElement).value as RagQualityIssueDTO["owner"]);
}
function statusLabel(value: RagQualityIssueDTO["status"]) {
  return statusOptions.find((item) => item.value === value)?.label ?? value;
}
function ownerLabel(value: RagQualityIssueDTO["owner"]) {
  return ownerOptions.find((item) => item.value === value)?.label ?? value;
}
function failureLabel(value: string) {
  return ({ chunk_semantic_split: "分块语义断裂", embedding_margin_low: "嵌入区分度不足", candidate_evidence_missed: "候选证据漏召回", reranker_evidence_dropped: "重排证据丢失", context_evidence_dropped: "上下文证据遗漏", context_truncated: "上下文截断" } as Record<string, string>)[value] ?? value;
}
function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}
function shortRevision(value: string | null) { return value ? value.slice(0, 8) : "—"; }

onMounted(load);
</script>

<template>
  <section class="space-y-4" aria-label="RAG 质量问题台账">
    <div class="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-[var(--color-border)] bg-white p-4">
      <div>
        <div class="flex items-center gap-2 text-sm font-medium"><ClipboardList :size="16" />质量问题台账</div>
        <p class="mt-1 max-w-2xl text-xs leading-5 text-[var(--color-muted)]">持续追踪门禁发现的问题、责任归属与回归验证结果。这里只展示脱敏定位信息。</p>
      </div>
      <button class="inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-xs disabled:opacity-50" :disabled="loading" @click="load"><RefreshCw :size="14" :class="loading ? 'animate-spin' : ''" />刷新</button>
    </div>

    <div class="grid grid-cols-2 gap-2 text-center text-xs sm:grid-cols-3 lg:grid-cols-6">
      <div v-for="item in [{ key: 'total', label: '全部' }, { key: 'open', label: '待处理' }, { key: 'in_progress', label: '处理中' }, { key: 'resolved', label: '待验证' }, { key: 'verified', label: '已验证' }, { key: 'dismissed', label: '已忽略' }]" :key="item.key" class="rounded-lg border bg-white p-3">
        <p class="text-lg font-semibold tabular-nums">{{ summary[item.key as keyof RagQualityIssueSummaryDTO] }}</p><p class="text-[var(--color-muted)]">{{ item.label }}</p>
      </div>
    </div>

    <div class="flex flex-wrap gap-2 rounded-xl border bg-white p-3">
      <select v-model="status" class="rounded border px-2 py-1.5 text-xs" @change="load"><option v-for="item in statusOptions" :key="item.value" :value="item.value">{{ item.label }}</option></select>
      <select v-model="owner" class="rounded border px-2 py-1.5 text-xs" @change="load"><option v-for="item in ownerOptions" :key="item.value" :value="item.value">{{ item.label }}</option></select>
      <select v-model="failureType" class="rounded border px-2 py-1.5 text-xs" @change="load">
        <option value="all">全部失败类型</option><option value="chunk_semantic_split">分块语义断裂</option><option value="embedding_margin_low">嵌入区分度不足</option><option value="candidate_evidence_missed">候选证据漏召回</option><option value="reranker_evidence_dropped">重排证据丢失</option><option value="context_evidence_dropped">上下文证据遗漏</option><option value="context_truncated">上下文截断</option>
      </select>
    </div>

    <div v-if="error" class="rounded-xl border border-red-200 bg-red-50 p-4 text-xs text-red-700">{{ error.message }}</div>
    <div v-if="loading && !issues.length" class="rounded-xl border bg-white p-8 text-center text-xs text-[var(--color-muted)]">正在读取问题台账…</div>
    <div v-else-if="!issues.length" class="rounded-xl border border-dashed bg-white p-8 text-center text-xs text-[var(--color-muted)]">当前筛选下没有质量问题</div>
    <div v-else class="space-y-3">
      <article v-for="item in issues" :key="item.issue.id" class="rounded-xl border border-[var(--color-border)] bg-white p-4 text-xs">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div><div class="flex flex-wrap items-center gap-2"><span class="font-medium">{{ failureLabel(item.issue.failure_type) }}</span><span class="rounded-full bg-gray-100 px-2 py-1 text-[11px]">{{ statusLabel(item.issue.status) }}</span></div><p class="mt-1 font-mono text-[11px] text-[var(--color-muted)]">Query {{ item.query_hash.slice(0, 12) }}</p></div>
          <button class="inline-flex items-center gap-1 rounded border px-2 py-1 text-blue-700" @click="emit('reviewTarget', { trace_id: item.trace_id, workspace_id: item.workspace_id })"><Eye :size="12" />打开审核</button>
        </div>
        <dl class="mt-3 grid gap-2 rounded-lg bg-gray-50 p-3 sm:grid-cols-2 lg:grid-cols-4">
          <div><dt class="text-[var(--color-muted)]">负责人</dt><dd class="mt-1">{{ ownerLabel(item.issue.owner) }}</dd></div><div><dt class="text-[var(--color-muted)]">出现次数</dt><dd class="mt-1">{{ item.issue.occurrence_count }} 次</dd></div><div><dt class="text-[var(--color-muted)]">首次 / 最近版本</dt><dd class="mt-1 font-mono">{{ shortRevision(item.first_seen_revision) }} / {{ shortRevision(item.last_seen_revision) }}</dd></div><div><dt class="text-[var(--color-muted)]">验证版本</dt><dd class="mt-1 font-mono">{{ shortRevision(item.verified_revision) }}</dd></div>
        </dl>
        <p v-if="item.issue.resolution_note" class="mt-3 rounded bg-blue-50 p-2 leading-5 text-blue-800">处理说明：{{ item.issue.resolution_note }}</p>
        <div class="mt-3 flex flex-wrap items-center gap-2 border-t pt-3">
          <select class="rounded border px-2 py-1" :value="item.issue.owner" :disabled="mutatingId === item.issue.id || !['open','in_progress'].includes(item.issue.status)" @change="changeOwner(item, $event)"><option v-for="option in ownerOptions.filter((value) => value.value !== 'all')" :key="option.value" :value="option.value">{{ option.label }}</option></select>
          <textarea v-if="item.issue.status === 'in_progress'" v-model="notes[item.issue.id]" maxlength="500" class="min-h-16 min-w-64 flex-1 rounded border p-2" placeholder="修复说明（标记待验证时必填）"></textarea>
          <button v-if="item.issue.status === 'open'" class="rounded bg-blue-600 px-2 py-1 text-white" :disabled="mutatingId === item.issue.id" @click="mutate(item, 'in_progress')">开始处理</button>
          <button v-if="item.issue.status === 'in_progress'" class="rounded bg-emerald-600 px-2 py-1 text-white disabled:opacity-50" :disabled="mutatingId === item.issue.id || !notes[item.issue.id]?.trim()" @click="mutate(item, 'resolved')">标记待验证</button>
          <span v-if="item.issue.status === 'resolved'" class="text-amber-700">等待同 cohort 门禁自动验证</span><span v-if="item.issue.status === 'verified'" class="text-emerald-700">定向回归已通过</span>
          <button v-if="['resolved','verified','dismissed'].includes(item.issue.status)" class="rounded border px-2 py-1" :disabled="mutatingId === item.issue.id" @click="mutate(item, item.issue.status === 'dismissed' ? 'open' : 'in_progress')">重新处理</button>
          <span class="ml-auto text-[11px] text-[var(--color-muted)]">记录版本 v{{ item.issue.version }} · {{ formatTime(item.issue.updated_at) }}</span>
        </div>
      </article>
    </div>
  </section>
</template>
