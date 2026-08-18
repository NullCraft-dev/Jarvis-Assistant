<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { AlertTriangle, CheckCircle2, Clock3, Eye, RefreshCw, ShieldCheck, TrendingDown, TrendingUp } from "@lucide/vue";
import type { AppError, RagQualityFailureTargetDTO, RagQualityGateInsightsDTO, RagQualityGateRunDTO, RagQualityIssueDTO } from "@jarvis/shared";
import { listRagQualityFailureTargets, listRagQualityGateRuns, updateRagQualityIssue } from "@/api/client";

const emit = defineEmits<{ reviewTarget: [target: RagQualityFailureTargetDTO] }>();

const runs = ref<RagQualityGateRunDTO[]>([]);
const insights = ref<RagQualityGateInsightsDTO | null>(null);
const loading = ref(false);
const error = ref<AppError | null>(null);
const targetError = ref<AppError | null>(null);
const targetLoading = ref<string | null>(null);
const expandedFailure = ref<string | null>(null);
const targets = ref<Record<string, RagQualityFailureTargetDTO[]>>({});
const issueNotes = ref<Record<string, string>>({});
const mutatingIssueId = ref<string | null>(null);
const latest = computed(() => runs.value[0] ?? null);
const failedChecks = computed(() => latest.value?.checks.filter((check) => !check.passed) ?? []);

const metricLabels: Record<string, string> = {
  "candidate.recall@5": "候选召回@5",
  "candidate.mrr": "候选 MRR",
  "reranker.recall@5": "重排召回@5",
  "reranker.mrr": "重排 MRR",
  "context.evidence_recall": "上下文证据召回",
  "context.truncated_rate": "上下文截断率",
};

async function load() {
  loading.value = true;
  error.value = null;
  const result = await listRagQualityGateRuns(20);
  if (result.ok) {
    runs.value = result.data.runs;
    insights.value = result.data.insights;
  }
  else error.value = result.error;
  loading.value = false;
}
async function toggleTargets(failureType: string) {
  if (expandedFailure.value === failureType) { expandedFailure.value = null; return; }
  expandedFailure.value = failureType;
  if (!latest.value || targets.value[failureType]) return;
  targetLoading.value = failureType; targetError.value = null;
  const result = await listRagQualityFailureTargets(latest.value.id, failureType);
  targetLoading.value = null;
  if (result.ok) targets.value = { ...targets.value, [failureType]: result.data.targets };
  else targetError.value = result.error;
}

function statusLabel(status: RagQualityGateRunDTO["status"]) {
  return status === "passed" ? "已通过" : status === "blocked" ? "已阻断" : "证据不足";
}
function formatMetric(value: number) {
  return `${(value * 100).toFixed(value === 1 ? 0 : 1)}%`;
}
function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}
function checkRequirement(check: RagQualityGateRunDTO["checks"][number]) {
  const value = check.required_minimum ?? check.required ?? check.maximum;
  return typeof value === "number" ? formatMetric(value) : value ?? "—";
}
function signedMetric(value: number) {
  const rendered = formatMetric(Math.abs(value));
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${rendered}`;
}
function directionLabel(direction: "improved" | "stable" | "regressed") {
  return direction === "improved" ? "改善" : direction === "regressed" ? "退化" : "持平";
}
function failureLabel(value: string) {
  return ({
    candidate_evidence_missed: "候选证据漏召回",
    reranker_evidence_dropped: "重排证据丢失",
    context_evidence_dropped: "上下文证据遗漏",
    context_truncated: "上下文截断",
  } as Record<string, string>)[value] ?? value;
}
function priorityLabel(value: string) {
  return ({ critical: "阻断", high: "高", medium: "中", low: "低" } as Record<string, string>)[value] ?? value;
}
function alertText(code: string, subject: string) {
  if (code === "status_regressed") return "发布状态从通过变为未通过";
  if (code === "check_failed") return `门禁检查失败：${subject}`;
  return `指标较上次退化：${metricLabels[subject] ?? subject}`;
}
function reviewStateLabel(value: RagQualityFailureTargetDTO["review_state"]) {
  return ({ privacy_required: "待隐私复核", privacy_rejected: "隐私已拒绝", label_review_required: "待标签确认", promotion_ready: "可生成候选", fixed_regression_sample: "已在固定回归集" })[value];
}
function issueStatusLabel(value: RagQualityIssueDTO["status"]) {
  return ({ open: "待处理", in_progress: "处理中", resolved: "等待回归验证", verified: "已验证", dismissed: "已忽略" })[value];
}
function issueOwnerLabel(value: RagQualityIssueDTO["owner"]) {
  return ({ data_quality: "数据与金标", candidate_recall: "候选召回", reranker: "重排", context_assembly: "上下文组装" })[value];
}
async function mutateIssue(target: RagQualityFailureTargetDTO, status: "open" | "in_progress" | "resolved" | "dismissed", owner = target.issue?.owner) {
  if (!target.issue || !owner) return;
  mutatingIssueId.value = target.issue.id; targetError.value = null;
  const result = await updateRagQualityIssue(target.issue.id, {
    expected_version: target.issue.version, owner, status,
    resolution_note: issueNotes.value[target.issue.id] ?? target.issue.resolution_note,
  });
  mutatingIssueId.value = null;
  if (!result.ok) { targetError.value = result.error; return; }
  for (const values of Object.values(targets.value)) {
    const current = values.find((value) => value.candidate_id === target.candidate_id);
    if (current) current.issue = result.data.issue;
  }
}
function changeOwner(target: RagQualityFailureTargetDTO, event: Event) {
  mutateIssue(target, target.issue!.status === "verified" ? "in_progress" : target.issue!.status, (event.target as HTMLSelectElement).value as RagQualityIssueDTO["owner"]);
}

onMounted(load);
</script>

<template>
  <section class="space-y-4" aria-label="RAG 发布门禁历史">
    <div class="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-[var(--color-border)] bg-white p-4">
      <div>
        <div class="flex items-center gap-2 text-sm font-medium"><ShieldCheck :size="16" />发布门禁</div>
        <p class="mt-1 max-w-2xl text-xs leading-5 text-[var(--color-muted)]">
          展示离线发布流程写入的脱敏结果。这里不能运行门禁、修改基线或晋升评测样本。
        </p>
      </div>
      <button class="inline-flex items-center gap-1.5 rounded-lg border border-[var(--color-border)] px-3 py-2 text-xs disabled:opacity-50" :disabled="loading" @click="load">
        <RefreshCw :size="14" :class="loading ? 'animate-spin' : ''" />刷新
      </button>
    </div>

    <div v-if="error" class="rounded-xl border border-red-200 bg-red-50 p-4 text-xs text-red-700">
      {{ error.message }}
    </div>
    <div v-else-if="loading && !latest" class="rounded-xl border border-[var(--color-border)] bg-white p-8 text-center text-xs text-[var(--color-muted)]">正在读取门禁结果…</div>
    <div v-else-if="!latest" class="rounded-xl border border-dashed border-[var(--color-border)] bg-white p-8 text-center text-xs text-[var(--color-muted)]">
      暂无门禁记录。下一次离线 RAG 发布门禁执行后会自动写入。
    </div>

    <template v-else>
      <div class="rounded-xl border bg-white p-4" :class="latest.status === 'passed' ? 'border-emerald-200' : 'border-amber-200'">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="flex items-start gap-3">
            <CheckCircle2 v-if="latest.status === 'passed'" class="mt-0.5 text-emerald-600" :size="20" />
            <AlertTriangle v-else class="mt-0.5 text-amber-600" :size="20" />
            <div>
              <div class="text-sm font-medium">最新结果：{{ statusLabel(latest.status) }}</div>
              <div class="mt-1 text-xs text-[var(--color-muted)]">{{ latest.gate_id }} · {{ latest.sample_count }} 条固定样本 · {{ formatTime(latest.generated_at) }}</div>
            </div>
          </div>
          <div class="rounded-md bg-gray-100 px-2 py-1 font-mono text-[11px] text-gray-600" :title="latest.revision">{{ latest.revision.slice(0, 8) }}</div>
        </div>
        <dl class="mt-4 grid gap-2 text-xs sm:grid-cols-2">
          <div class="rounded-lg bg-gray-50 p-3"><dt class="text-[var(--color-muted)]">评测 cohort</dt><dd class="mt-1 font-medium">{{ latest.cohort_id }}</dd></div>
          <div class="rounded-lg bg-gray-50 p-3"><dt class="text-[var(--color-muted)]">比较基线</dt><dd class="mt-1 font-medium">{{ latest.baseline_id }}</dd></div>
        </dl>
      </div>

      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div v-for="(value, metric) in latest.metrics" :key="metric" class="rounded-xl border border-[var(--color-border)] bg-white p-4">
          <div class="text-xs text-[var(--color-muted)]">{{ metricLabels[metric] ?? metric }}</div>
          <div class="mt-2 text-xl font-semibold tabular-nums">{{ formatMetric(value) }}</div>
        </div>
      </div>

      <div class="rounded-xl border border-[var(--color-border)] bg-white p-4">
        <div class="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div class="text-sm font-medium">质量趋势与退化提示</div>
            <p class="mt-1 text-xs text-[var(--color-muted)]">只比较相同门禁和固定 cohort，避免回归集变化制造假趋势。</p>
          </div>
          <span class="rounded-full bg-gray-100 px-2 py-1 text-[11px] text-[var(--color-muted)]">{{ insights?.compatible_history_count ?? 0 }} 次可比运行</span>
        </div>
        <div v-if="insights?.comparison_state === 'insufficient_history'" class="mt-3 rounded-lg bg-blue-50 p-3 text-xs leading-5 text-blue-700">
          当前只有一次可比运行。第二次相同 cohort 门禁完成后，系统会自动生成指标增减和退化提醒。
        </div>
        <div v-else-if="insights" class="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          <div v-for="trend in insights.metric_trends" :key="trend.metric_id" class="rounded-lg border border-[var(--color-border)] p-3">
            <div class="text-[11px] text-[var(--color-muted)]">{{ metricLabels[trend.metric_id] ?? trend.metric_id }}</div>
            <div class="mt-2 flex items-center justify-between gap-2 text-xs">
              <span class="font-medium">{{ formatMetric(trend.current) }}</span>
              <span class="inline-flex items-center gap-1" :class="trend.direction === 'regressed' ? 'text-red-600' : trend.direction === 'improved' ? 'text-emerald-600' : 'text-[var(--color-muted)]'">
                <TrendingDown v-if="trend.direction === 'regressed'" :size="13" />
                <TrendingUp v-else-if="trend.direction === 'improved'" :size="13" />
                {{ directionLabel(trend.direction) }} {{ signedMetric(trend.delta) }}
              </span>
            </div>
          </div>
        </div>
        <div v-if="insights?.alerts.length" class="mt-3 space-y-2">
          <div v-for="alert in insights.alerts" :key="`${alert.code}:${alert.subject_id}`" class="flex gap-2 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
            <AlertTriangle :size="14" class="mt-0.5 shrink-0" />{{ alertText(alert.code, alert.subject_id) }}
          </div>
        </div>
        <div v-else-if="insights?.comparison_state === 'ready'" class="mt-3 text-xs text-emerald-700">没有检测到门禁失败或显著指标退化。</div>
      </div>

      <div v-if="insights?.failure_clusters.length" class="overflow-hidden rounded-xl border border-[var(--color-border)] bg-white">
        <div class="border-b border-[var(--color-border)] px-4 py-3">
          <div class="text-sm font-medium">失败簇优先级</div>
          <p class="mt-1 text-xs text-[var(--color-muted)]">聚合门禁失败率用于安排诊断顺序，不会自动修改检索策略或金标。</p>
        </div>
        <div v-for="cluster in insights.failure_clusters" :key="cluster.failure_type" class="border-b border-[var(--color-border)] px-4 py-3 last:border-b-0">
          <div class="flex flex-wrap items-center justify-between gap-3">
           <div>
            <div class="flex items-center gap-2 text-xs font-medium">
              {{ failureLabel(cluster.failure_type) }}
              <span class="rounded-full px-2 py-0.5 text-[10px]" :class="cluster.priority === 'critical' ? 'bg-red-100 text-red-700' : cluster.priority === 'high' ? 'bg-amber-100 text-amber-700' : cluster.priority === 'medium' ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600'">{{ priorityLabel(cluster.priority) }}</span>
            </div>
            <div class="mt-1 text-[11px] text-[var(--color-muted)]">最近 {{ cluster.latest_count }} 条 · {{ cluster.occurrence_count }} 次运行出现</div>
           </div>
           <div class="flex items-center gap-3">
            <div class="text-right text-xs"><div class="font-medium tabular-nums">{{ formatMetric(cluster.latest_rate) }}</div><div class="mt-1 text-[11px] text-[var(--color-muted)]">阈值 {{ formatMetric(cluster.threshold) }}</div></div>
            <button class="rounded border px-2 py-1 text-xs text-blue-700 disabled:opacity-50" :disabled="targetLoading === cluster.failure_type" @click="toggleTargets(cluster.failure_type)">{{ expandedFailure === cluster.failure_type ? "收起" : "查看样本" }}</button>
           </div>
          </div>
          <div v-if="expandedFailure === cluster.failure_type" class="mt-3 space-y-2 border-t pt-3">
            <p v-if="targetLoading === cluster.failure_type" class="text-xs text-[var(--color-muted)]">正在定位脱敏失败样本…</p>
            <p v-else-if="targetError" class="rounded bg-red-50 p-2 text-xs text-red-700">{{ targetError.message }}</p>
            <p v-else-if="!targets[cluster.failure_type]?.length" class="text-xs text-[var(--color-muted)]">该历史门禁尚未保存可定位样本；重新执行门禁后会自动生成。</p>
            <article v-for="target in targets[cluster.failure_type]" :key="target.candidate_id" class="rounded-lg bg-gray-50 p-3 text-xs">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div><p class="font-medium">Query {{ target.query_hash.slice(0, 12) }}</p><p class="mt-1 text-[11px] text-[var(--color-muted)]">{{ target.suspected_stage }} 阶段 · {{ reviewStateLabel(target.review_state) }}</p></div>
                <button class="inline-flex items-center gap-1 rounded border bg-white px-2 py-1 text-blue-700" @click="emit('reviewTarget', target)"><Eye :size="12" />打开审核</button>
              </div>
              <div v-if="target.issue" class="mt-3 border-t pt-3">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="rounded-full bg-white px-2 py-1 font-medium">{{ issueStatusLabel(target.issue.status) }}</span>
                  <span class="text-[11px] text-[var(--color-muted)]">出现 {{ target.issue.occurrence_count }} 次</span>
                  <select class="rounded border bg-white px-2 py-1" :value="target.issue.owner" :disabled="mutatingIssueId === target.issue.id || !['open','in_progress'].includes(target.issue.status)" @change="changeOwner(target, $event)">
                    <option value="data_quality">{{ issueOwnerLabel('data_quality') }}</option><option value="candidate_recall">{{ issueOwnerLabel('candidate_recall') }}</option><option value="reranker">{{ issueOwnerLabel('reranker') }}</option><option value="context_assembly">{{ issueOwnerLabel('context_assembly') }}</option>
                  </select>
                </div>
                <textarea v-if="target.issue.status === 'in_progress'" v-model="issueNotes[target.issue.id]" maxlength="500" class="mt-2 min-h-16 w-full rounded border bg-white p-2" placeholder="修复说明（标记待验证时必填）"></textarea>
                <div class="mt-2 flex flex-wrap justify-end gap-2">
                  <button v-if="target.issue.status === 'open'" class="rounded bg-blue-600 px-2 py-1 text-white" :disabled="mutatingIssueId === target.issue.id" @click="mutateIssue(target, 'in_progress')">开始处理</button>
                  <button v-if="target.issue.status === 'in_progress'" class="rounded bg-emerald-600 px-2 py-1 text-white" :disabled="mutatingIssueId === target.issue.id || !(issueNotes[target.issue.id]?.trim())" @click="mutateIssue(target, 'resolved')">标记待验证</button>
                  <span v-if="target.issue.status === 'resolved'" class="text-[11px] text-amber-700">下一次相同 cohort 门禁未再出现时自动验证</span>
                  <span v-if="target.issue.status === 'verified'" class="text-[11px] text-emerald-700">定向回归已通过</span>
                  <button v-if="['resolved','verified','dismissed'].includes(target.issue.status)" class="rounded border bg-white px-2 py-1" :disabled="mutatingIssueId === target.issue.id" @click="mutateIssue(target, target.issue.status === 'dismissed' ? 'open' : 'in_progress')">重新处理</button>
                </div>
              </div>
              <p v-else class="mt-2 text-[11px] text-[var(--color-muted)]">重新执行当前版本门禁后会自动创建治理记录。</p>
            </article>
          </div>
        </div>
      </div>

      <div v-if="failedChecks.length" class="rounded-xl border border-amber-200 bg-amber-50 p-4">
        <div class="text-sm font-medium text-amber-900">未通过检查</div>
        <div v-for="check in failedChecks" :key="check.check_id" class="mt-2 flex flex-wrap justify-between gap-2 text-xs text-amber-800">
          <span>{{ check.check_id }}</span><span>实际 {{ check.actual ?? "—" }} / 要求 {{ checkRequirement(check) }}</span>
        </div>
      </div>

      <div class="overflow-hidden rounded-xl border border-[var(--color-border)] bg-white">
        <div class="border-b border-[var(--color-border)] px-4 py-3 text-sm font-medium">最近运行</div>
        <div v-for="run in runs" :key="run.id" class="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] px-4 py-3 last:border-b-0">
          <div class="flex items-center gap-2">
            <Clock3 :size="14" class="text-[var(--color-muted)]" />
            <div><div class="text-xs font-medium">{{ statusLabel(run.status) }}</div><div class="mt-0.5 text-[11px] text-[var(--color-muted)]">{{ formatTime(run.generated_at) }} · {{ run.sample_count }} 条样本</div></div>
          </div>
          <span class="font-mono text-[11px] text-[var(--color-muted)]">{{ run.revision.slice(0, 8) }}</span>
        </div>
      </div>
    </template>
  </section>
</template>
