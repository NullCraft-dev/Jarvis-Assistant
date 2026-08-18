<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { Ban, CirclePlay, Database, FileSearch, LoaderCircle, RotateCcw, ShieldCheck, Trash2, TriangleAlert, Upload, XCircle } from "@lucide/vue";
import type { ID, RagDocumentDTO, RagDocumentStatus, RagJobProgressDTO } from "@jarvis/shared";
import { useRagDocumentStore } from "@/stores/ragDocumentStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import {
  isRagBatchEligible,
  RAG_BATCH_SELECTION_LIMIT,
  ragBatchActionLabels,
  ragBatchImpact,
  ragDocumentStatusLabels,
  ragJobStatusLabels,
  sourceSummary,
  vectorCountText,
  type RagBatchAction,
} from "@/features/knowledge/ragDocumentPresentation";

const store = useRagDocumentStore();
const props = withDefaults(defineProps<{
  focusedDocumentId?: ID;
  focusedChunkId?: ID;
}>(), {
  focusedDocumentId: "",
  focusedChunkId: "",
});
const workspaces = useWorkspaceStore();
const workspaceName = computed(() => workspaces.selectedWorkspace ? workspaces.displayName(workspaces.selectedWorkspace) : "未选择工作区");
const isInitialLoading = computed(() =>
  store.loading && (
    !store.documents.length || store.loadedWorkspaceId !== workspaces.selectedWorkspaceId
  )
);
const fileInput = ref<HTMLInputElement | null>(null);
const selectedIds = ref<ID[]>([]);
const selectionError = ref("");
const batchPreviewAction = ref<RagBatchAction | null>(null);
const nonDeleteBatchActions: RagBatchAction[] = ["enable", "disable", "restart", "cancel"];
let pollTimer: ReturnType<typeof setTimeout> | undefined;
const focusedDocumentId = computed(() => props.focusedDocumentId);
const focusedChunkId = computed(() => props.focusedChunkId);

const selectedDocuments = computed(() => {
  const ids = new Set(selectedIds.value);
  return store.documents.filter((document) => ids.has(document.id));
});
const eligibleBatchDocuments = computed(() =>
  batchPreviewAction.value
    ? selectedDocuments.value.filter((document) => isRagBatchEligible(document, batchPreviewAction.value!))
    : []
);
const skippedBatchDocuments = computed(() =>
  batchPreviewAction.value
    ? selectedDocuments.value.filter((document) => !isRagBatchEligible(document, batchPreviewAction.value!))
    : []
);
const batchDeletePosition = computed(() =>
  store.batchDeleteTotal
    ? store.batchDeleteTotal - store.batchDeleteQueue.length
    : 0
);

function toggleSelection(documentId: ID) {
  if (selectedIds.value.includes(documentId)) {
    selectedIds.value = selectedIds.value.filter((id) => id !== documentId);
    selectionError.value = "";
    return;
  }
  if (selectedIds.value.length >= RAG_BATCH_SELECTION_LIMIT) {
    selectionError.value = `每次最多选择 ${RAG_BATCH_SELECTION_LIMIT} 个文档`;
    return;
  }
  selectedIds.value = [...selectedIds.value, documentId];
}

function selectFirstBatch() {
  selectedIds.value = store.documents.slice(0, RAG_BATCH_SELECTION_LIMIT).map((document) => document.id);
  selectionError.value = store.documents.length > RAG_BATCH_SELECTION_LIMIT
    ? `已按上限选择前 ${RAG_BATCH_SELECTION_LIMIT} 个文档`
    : "";
}

function clearSelection() {
  selectedIds.value = [];
  selectionError.value = "";
}

function openBatchPreview(action: RagBatchAction) {
  if (!selectedDocuments.value.length || store.batchRunning) return;
  batchPreviewAction.value = action;
}

async function confirmBatch() {
  const action = batchPreviewAction.value;
  if (!action) return;
  const snapshot = [...selectedDocuments.value];
  batchPreviewAction.value = null;
  if (action === "delete") {
    await store.startBatchDelete(workspaces.selectedWorkspaceId, snapshot);
  } else {
    await store.executeBatch(workspaces.selectedWorkspaceId, snapshot, action);
  }
  clearSelection();
}

function chooseFile() { fileInput.value?.click(); }
async function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  await store.upload(workspaces.selectedWorkspaceId, file);
}
async function resolveUpload(decision: "allow_once" | "deny") {
  await store.resolveUpload(decision);
}
async function restartDocument(document: RagDocumentDTO) {
  if (!window.confirm(`重新执行“${document.title}”的 RAG 解析、分块和向量化？`)) return;
  await store.restart(workspaces.selectedWorkspaceId, document.id, document.version);
}
async function toggleDocument(document: RagDocumentDTO) {
  const enabled = document.status === "disabled";
  const action = enabled ? "启用" : "停用";
  if (!window.confirm(`${action}“${document.title}”的 RAG 检索？`)) return;
  await store.setEnabled(
    workspaces.selectedWorkspaceId,
    document.id,
    document.version,
    enabled,
  );
}
async function cancelDocument(document: RagDocumentDTO) {
  if (!window.confirm(`取消“${document.title}”当前正在运行的 RAG 作业？已完成的阶段不会被视为可检索索引。`)) return;
  await store.cancel(workspaces.selectedWorkspaceId, document.id, document.version);
}
async function requestDelete(document: RagDocumentDTO) {
  await store.requestDelete(workspaces.selectedWorkspaceId, document);
}
async function resolveDelete(decision: "allow_once" | "deny") {
  await store.resolveDelete(decision);
}
function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer);
  if (store.documents.some((document) => document.status === "indexing")) {
    pollTimer = setTimeout(async () => {
      await store.load(workspaces.selectedWorkspaceId);
      schedulePoll();
    }, 3000);
  }
}
watch(
  () => `${workspaces.selectedWorkspaceId ?? ""}|${store.documents.map((document) => `${document.id}:${document.status}:${document.latest_job?.status ?? ""}`).join("|")}`,
  schedulePoll,
  { immediate: true },
);
watch(
  () => `${focusedDocumentId.value}|${store.documents.map((document) => document.id).join("|")}`,
  async () => {
    if (!focusedDocumentId.value) return;
    await nextTick();
    document.getElementById(`rag-document-${focusedDocumentId.value}`)?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
  },
  { immediate: true },
);
watch(() => workspaces.selectedWorkspaceId, clearSelection);
watch(
  () => store.documents.map((document) => document.id).join("|"),
  () => {
    const available = new Set(store.documents.map((document) => document.id));
    selectedIds.value = selectedIds.value.filter((id) => available.has(id));
  },
);
onBeforeUnmount(() => { if (pollTimer) clearTimeout(pollTimer); });

function statusClass(status: RagDocumentStatus) {
  if (status === "ready") return "bg-emerald-50 text-emerald-700";
  if (status === "failed") return "bg-red-50 text-red-700";
  if (status === "indexing") return "bg-blue-50 text-blue-700";
  return "bg-gray-100 text-gray-600";
}
function formatTime(value?: string) {
  return value ? new Date(value).toLocaleString() : "—";
}
function formatBytes(value: unknown) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
}
const executorLabels: Record<string, string> = {
  pymupdf: "PyMuPDF 原生提取",
  "paddleocr-vl": "PaddleOCR-VL 视觉解析",
  chunker: "多模态分块器",
  "openai-embedding": "OpenAI Embedding",
};
const emptyProgress: RagJobProgressDTO = {
  page_count: 0,
  native_extraction_done: false,
  visual_pages_total: 0,
  visual_pages_completed: 0,
  visual_route_counts: {},
  chunks_total: 0,
  embedding_total: 0,
  embedding_completed: 0,
};
function hasRecordedProgress(progress?: RagJobProgressDTO) {
  return Boolean(
    progress?.active_executor
    || progress?.page_count
    || progress?.native_extraction_done
    || progress?.visual_pages_total
    || progress?.visual_pages_completed
    || Object.values(progress?.visual_route_counts ?? {}).some((count) => count > 0)
    || progress?.chunks_total
    || progress?.embedding_total
    || progress?.embedding_completed
  );
}
const routeReasonLabels: Record<string, string> = {
  ocr_required: "原生文字不足",
  complex_image: "复杂图片",
  complex_table: "复杂表格",
};
const staleReasonLabels: Record<string, string> = {
  ingestion_policy_version: "入库策略",
  parser_version: "解析器",
  chunker_version: "分块器",
  embedding_provider: "Embedding Provider",
  embedding_model: "Embedding 模型",
  embedding_dimensions: "向量维度",
};
function staleReasonText(document: RagDocumentDTO) {
  return document.index_stale_reasons.map((reason) => staleReasonLabels[reason] ?? reason).join("、");
}
function routeReasonText(document: (typeof store.documents)[number]) {
  const counts = document.latest_job?.progress?.visual_route_counts ?? {};
  const entries = Object.entries(counts).filter(([, count]) => count > 0);
  if (!entries.length) return "无需视觉增强";
  return entries
    .map(([reason, count]) => `${routeReasonLabels[reason] ?? reason} ${count} 页`)
    .join(" · ");
}
function progressText(document: (typeof store.documents)[number]) {
  const job = document.latest_job;
  if (!job) return "无进度";
  if (!hasRecordedProgress(job.progress)) {
    if (job.status === "completed") return `${document.chunk_count} 个分块已索引（历史作业）`;
    if (["parsing", "chunking", "embedding"].includes(job.status)) return "本次旧作业未记录详细进度";
    if (job.status === "queued") return "等待 Worker 领取";
    return "—";
  }
  const progress = job.progress ?? emptyProgress;
  if (job.status === "parsing") {
    if (progress.visual_pages_total > 0) {
      return `视觉页 ${progress.visual_pages_completed}/${progress.visual_pages_total} · 全文 ${progress.page_count} 页`;
    }
    if (progress.native_extraction_done) return `原生提取完成 · 全文 ${progress.page_count} 页`;
    return "正在读取 PDF 页面";
  }
  if (job.status === "chunking") return progress.chunks_total ? `已生成 ${progress.chunks_total} 个分块` : "正在生成分块";
  if (job.status === "embedding") return `${progress.embedding_completed}/${progress.embedding_total} 个向量`;
  if (job.status === "completed") return `${progress.embedding_completed} 个向量已入库`;
  return "—";
}
function executorText(document: (typeof store.documents)[number]) {
  const job = document.latest_job;
  if (!job) return "尚未领取";
  if (!hasRecordedProgress(job.progress) && ["parsing", "chunking", "embedding"].includes(job.status)) {
    return "本次旧作业未记录";
  }
  const progress = job.progress ?? emptyProgress;
  if (progress.active_executor) {
    return executorLabels[progress.active_executor] || progress.active_executor;
  }
  if (["completed", "failed", "cancelled"].includes(job.status)) return "已结束";
  return "尚未领取";
}
</script>

<template>
  <section class="space-y-3">
    <div class="flex items-start justify-between gap-4">
      <div>
        <div class="flex items-center gap-2">
          <Database :size="16" />
          <h2 class="text-sm font-medium">RAG 文档</h2>
          <span class="text-xs text-[var(--color-muted)]">{{ store.documents.length }}</span>
          <span v-if="store.loading && !isInitialLoading" class="flex items-center gap-1 text-[11px] text-[var(--color-muted)]"><LoaderCircle :size="11" class="animate-spin" />更新中</span>
        </div>
        <p class="mt-1 text-xs text-[var(--color-muted)]">{{ workspaceName }} · 文档会被解析、分块并向量化，用于领域检索。</p>
      </div>
      <input ref="fileInput" type="file" accept="application/pdf,.pdf" class="hidden" @change="onFileChange" />
      <button class="flex shrink-0 items-center gap-1 rounded bg-violet-600 px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="!workspaces.selectedWorkspaceId || store.uploading" @click="chooseFile">
        <LoaderCircle v-if="store.uploading" :size="13" class="animate-spin" /><Upload v-else :size="13" />
        {{ store.uploading ? "上传中…" : "上传 PDF" }}
      </button>
    </div>

    <p v-if="store.uploadMessage" class="rounded border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-700">{{ store.uploadMessage }}</p>
    <p v-if="focusedDocumentId" class="rounded border border-violet-200 bg-violet-50 p-3 text-xs text-violet-700">
      已定位到回答引用的来源文档<span v-if="focusedChunkId"> · 证据分块 {{ focusedChunkId }}</span>
    </p>
    <div v-if="store.error" class="flex min-w-0 items-start justify-between gap-3 rounded border border-red-200 bg-red-50 p-3 text-xs text-red-700">
      <span class="flex min-w-0 items-start gap-2">
        <TriangleAlert :size="14" class="mt-0.5 shrink-0" />
        <span class="min-w-0 break-words">
          {{ store.error }}
          <span v-if="store.operationError" class="mt-1 block font-mono text-[11px] opacity-75">
            {{ store.operationError.code }} · {{ store.operationError.recoverable ? "可以刷新后重试" : "需要检查文档状态或服务配置" }}
          </span>
        </span>
      </span>
      <button class="shrink-0 rounded border border-red-200 bg-white px-2.5 py-1" @click="store.load(workspaces.selectedWorkspaceId)">刷新列表</button>
    </div>

    <section v-if="store.batchResults.length" class="rounded border border-slate-200 bg-slate-50 p-3 text-xs" aria-label="批量操作结果">
      <div class="flex items-center justify-between gap-3">
        <p class="font-medium">{{ store.batchAction ? ragBatchActionLabels[store.batchAction] : "批量操作" }}结果</p>
        <button v-if="!store.batchRunning" class="text-[var(--color-muted)] hover:text-[var(--color-text)]" @click="store.clearBatchResults()">关闭</button>
      </div>
      <div class="mt-2 grid gap-1.5 sm:grid-cols-2">
        <div v-for="result in store.batchResults" :key="`${result.document_id}-${result.status}`" class="min-w-0 rounded border bg-white px-2.5 py-2">
          <div class="flex items-start justify-between gap-2">
            <span class="min-w-0 truncate font-medium" :title="result.title">{{ result.title }}</span>
            <span
              class="shrink-0 rounded px-1.5 py-0.5 text-[10px]"
              :class="result.status === 'succeeded' ? 'bg-emerald-50 text-emerald-700' : result.status === 'failed' ? 'bg-red-50 text-red-700' : 'bg-gray-100 text-gray-600'"
            >{{ result.status === "succeeded" ? "成功" : result.status === "failed" ? "失败" : "跳过" }}</span>
          </div>
          <p class="mt-1 break-words text-[var(--color-muted)]">{{ result.message }}</p>
          <p v-if="result.error_code" class="mt-1 break-all font-mono text-[10px] text-red-600">{{ result.error_code }}</p>
        </div>
      </div>
    </section>

    <div v-if="!workspaces.selectedWorkspaceId" class="rounded border border-dashed p-6 text-center text-xs text-[var(--color-muted)]">
      请先在顶部选择一个工作区。
    </div>
    <div v-else-if="isInitialLoading" class="flex items-center justify-center gap-2 rounded border border-dashed p-6 text-xs text-[var(--color-muted)]">
      <LoaderCircle :size="14" class="animate-spin" />正在读取 RAG 文档…
    </div>
    <div v-else-if="!store.documents.length" class="rounded border border-dashed p-6 text-center text-xs text-[var(--color-muted)]">
      当前工作区暂无 RAG 文档。
    </div>
    <div v-else class="space-y-2">
      <div class="rounded-lg border bg-white p-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex flex-wrap items-center gap-2 text-xs">
            <strong>批量运维</strong>
            <span class="text-[var(--color-muted)]">已选择 {{ selectedIds.length }}/{{ RAG_BATCH_SELECTION_LIMIT }}</span>
            <button class="rounded border px-2 py-1 hover:bg-gray-50" :disabled="store.batchRunning" @click="selectFirstBatch">选择前 {{ Math.min(store.documents.length, RAG_BATCH_SELECTION_LIMIT) }} 项</button>
            <button v-if="selectedIds.length" class="px-1 py-1 text-[var(--color-muted)]" :disabled="store.batchRunning" @click="clearSelection">清除</button>
          </div>
          <div class="flex flex-wrap gap-1.5">
            <button v-for="action in nonDeleteBatchActions" :key="action" class="rounded border px-2 py-1 text-xs hover:bg-gray-50 disabled:opacity-40" :disabled="!selectedIds.length || store.batchRunning" @click="openBatchPreview(action)">{{ ragBatchActionLabels[action] }}</button>
            <button class="rounded border border-red-200 px-2 py-1 text-xs text-red-700 hover:bg-red-50 disabled:opacity-40" :disabled="!selectedIds.length || store.batchRunning" @click="openBatchPreview('delete')">批量删除</button>
          </div>
        </div>
        <p v-if="selectionError" class="mt-2 text-xs text-amber-700">{{ selectionError }}</p>
        <p v-if="store.batchRunning && store.batchAction !== 'delete'" class="mt-2 flex items-center gap-1 text-xs text-violet-700"><LoaderCircle :size="12" class="animate-spin" />正在按选择顺序执行；已完成结果会逐项保留。</p>
      </div>

      <article
        v-for="document in store.documents"
        :id="`rag-document-${document.id}`"
        :key="document.id"
        class="rounded-lg border bg-white p-4 transition"
        :class="focusedDocumentId === document.id ? 'border-violet-400 ring-2 ring-violet-100' : ''"
      >
        <div class="flex items-start justify-between gap-4">
          <div class="flex min-w-0 items-start gap-3">
            <input
              type="checkbox"
              class="mt-0.5 h-4 w-4 shrink-0 accent-violet-600"
              :checked="selectedIds.includes(document.id)"
              :disabled="store.batchRunning || (!selectedIds.includes(document.id) && selectedIds.length >= RAG_BATCH_SELECTION_LIMIT)"
              :aria-label="`选择 ${document.title}`"
              @change="toggleSelection(document.id)"
            />
            <FileSearch :size="17" class="mt-0.5 shrink-0 text-violet-500" />
            <div class="min-w-0">
              <p class="truncate text-sm font-medium" :title="document.title">{{ document.title }}</p>
              <p class="mt-1 break-all text-xs text-[var(--color-muted)]">{{ sourceSummary(document) }}</p>
            </div>
          </div>
          <span class="shrink-0 rounded px-2 py-1 text-[11px]" :class="statusClass(document.status)">{{ ragDocumentStatusLabels[document.status] }}</span>
        </div>
        <p v-if="document.index_state === 'stale'" class="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          当前索引已过期（{{ staleReasonText(document) }}），仍可检索旧索引；建议重新索引后使用新版本。
        </p>
        <div class="mt-3 grid gap-2 border-t pt-3 text-xs text-[var(--color-muted)] sm:grid-cols-2 lg:grid-cols-4">
          <p>文档版本：<span class="font-mono text-[var(--color-text)]">v{{ document.version }}</span></p>
          <p>索引版本：<span class="text-[var(--color-text)]">{{ document.ingestion_policy_version || "尚未生成" }}</span></p>
          <p>分块计数：<span class="text-[var(--color-text)]">{{ document.chunk_count }}</span></p>
          <p>向量计数：<span class="text-[var(--color-text)]">{{ vectorCountText(document) }}</span></p>
          <p>作业阶段：<span class="text-[var(--color-text)]">{{ document.latest_job ? ragJobStatusLabels[document.latest_job.status] : "无作业记录" }}</span></p>
          <p>尝试次数：<span class="text-[var(--color-text)]">{{ document.latest_job ? `${document.latest_job.attempts}/${document.latest_job.max_attempts} · Embedding ${document.latest_job.embedding_attempts}/${document.latest_job.embedding_max_attempts}` : "—" }}</span></p>
          <p>当前执行器：<span class="text-[var(--color-text)]">{{ executorText(document) }}</span></p>
          <p>真实进度：<span class="text-[var(--color-text)]">{{ progressText(document) }}</span></p>
          <p>最近更新：<span class="text-[var(--color-text)]">{{ formatTime(document.updated_at) }}</span></p>
        </div>
        <p v-if="document.latest_job?.error_code" class="mt-3 rounded bg-red-50 px-3 py-2 text-xs text-red-700">错误代码：{{ document.latest_job.error_code }}</p>
        <div class="mt-3 flex flex-wrap justify-end gap-2 border-t pt-3">
          <button v-if="document.status === 'indexing'" class="flex items-center gap-1 rounded border border-amber-200 px-2.5 py-1.5 text-xs text-amber-700 disabled:opacity-50" :disabled="store.mutatingDocumentId !== null" @click="cancelDocument(document)">
            <LoaderCircle v-if="store.mutatingDocumentId === document.id" :size="12" class="animate-spin" /><XCircle v-else :size="12" />
            {{ store.mutatingDocumentId === document.id ? "取消中…" : "取消作业" }}
          </button>
          <button v-if="document.status === 'ready' || document.status === 'disabled'" class="flex items-center gap-1 rounded border px-2.5 py-1.5 text-xs text-[var(--color-text)] disabled:opacity-50" :disabled="store.mutatingDocumentId !== null" @click="toggleDocument(document)">
            <CirclePlay v-if="document.status === 'disabled'" :size="12" /><Ban v-else :size="12" />
            {{ document.status === "disabled" ? "启用检索" : "停用检索" }}
          </button>
          <button v-if="document.status !== 'indexing' && document.status !== 'disabled'" class="flex items-center gap-1 rounded border px-2.5 py-1.5 text-xs text-[var(--color-text)] disabled:opacity-50" :disabled="store.restartingDocumentId !== null || store.mutatingDocumentId !== null" @click="restartDocument(document)">
            <LoaderCircle v-if="store.restartingDocumentId === document.id" :size="12" class="animate-spin" /><RotateCcw v-else :size="12" />
            {{ store.restartingDocumentId === document.id ? "重新排队中…" : document.index_state === "stale" ? "升级索引" : "重新执行" }}
          </button>
          <button v-if="document.status !== 'indexing'" class="flex items-center gap-1 rounded border border-red-200 px-2.5 py-1.5 text-xs text-red-700 disabled:opacity-50" :disabled="store.mutatingDocumentId !== null || store.restartingDocumentId !== null" @click="requestDelete(document)">
            <LoaderCircle v-if="store.mutatingDocumentId === document.id" :size="12" class="animate-spin" /><Trash2 v-else :size="12" />
            永久删除
          </button>
        </div>
        <details class="mt-3 text-xs text-[var(--color-muted)]">
          <summary class="cursor-pointer select-none font-medium text-[var(--color-text)]">文档与最近作业详情</summary>
          <div class="mt-2 grid gap-2 break-all rounded bg-gray-50 p-3 sm:grid-cols-2">
            <p>可信来源：{{ sourceSummary(document) }}</p><p>Document ID：{{ document.id }}</p>
            <p>Parser：{{ document.parser_version || "—" }}</p><p>Chunker：{{ document.chunker_version || "—" }}</p>
            <p>Embedding：{{ document.embedding_provider && document.embedding_model ? `${document.embedding_provider} / ${document.embedding_model}` : "尚未生成" }}</p>
            <p>向量维度：{{ document.embedding_dimensions ?? "—" }}</p>
            <p>索引时间：{{ formatTime(document.indexed_at) }}</p><p>创建时间：{{ formatTime(document.created_at) }}</p>
            <p>视觉路由：{{ routeReasonText(document) }}</p><p>目标索引：{{ JSON.stringify(document.index_target) }}</p>
            <template v-if="document.latest_job">
              <p>最近 Job ID：{{ document.latest_job.id }}</p><p>Job 更新：{{ formatTime(document.latest_job.updated_at) }}</p>
              <p>开始时间：{{ formatTime(document.latest_job.started_at) }}</p><p>结束时间：{{ formatTime(document.latest_job.completed_at || document.latest_job.failed_at) }}</p>
              <p v-if="document.latest_job.next_retry_at">下次重试：{{ formatTime(document.latest_job.next_retry_at) }}</p>
            </template>
          </div>
        </details>
      </article>
    </div>
  </section>

  <div v-if="store.uploadRequest" class="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4" role="dialog" aria-modal="true" aria-labelledby="rag-upload-title">
    <div class="w-full max-w-lg rounded-xl bg-white p-5 shadow-xl">
      <div class="flex items-center gap-2 text-violet-700"><ShieldCheck :size="18" /><h3 id="rag-upload-title" class="font-medium">确认 RAG 文档入库</h3></div>
      <p class="mt-3 text-sm text-[var(--color-text)]">{{ store.uploadRequest.action_summary }}</p>
      <div class="mt-3 rounded border border-violet-200 bg-violet-50 p-3 text-xs leading-5 text-violet-900">
        <p>风险等级：L2（限定范围写入）</p>
        <p>工具：<span class="font-mono">{{ store.uploadRequest.tool_name }}</span></p>
        <p>授权范围：仅本次、仅当前工作区、仅下列文件内容。</p>
        <p>文件：{{ store.uploadRequest.arguments_summary.filename }}</p>
        <p>大小：{{ formatBytes(store.uploadRequest.arguments_summary.size_bytes) }}</p>
        <p class="break-all">SHA-256：<span class="font-mono">{{ store.uploadRequest.arguments_summary.content_sha256 }}</span></p>
      </div>
      <p class="mt-3 text-xs leading-5 text-[var(--color-muted)]">批准后才会创建 Artifact、RAG 文档与入库作业；拒绝不会产生这些副作用。文件名称、大小或摘要变化时，授权自动失效。</p>
      <p v-if="store.error" class="mt-3 break-words rounded border border-red-200 bg-red-50 p-2.5 text-xs text-red-700">{{ store.error }}</p>
      <div class="mt-5 flex justify-end gap-2">
        <button class="rounded border px-3 py-2 text-xs disabled:opacity-50" :disabled="store.uploading" @click="resolveUpload('deny')">拒绝</button>
        <button class="rounded bg-violet-600 px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="store.uploading" @click="resolveUpload('allow_once')">
          {{ store.uploading ? "处理中…" : store.uploadRequest.status === "approved" ? "重试已授权上传" : "仅允许本次" }}
        </button>
      </div>
    </div>
  </div>

  <div v-if="batchPreviewAction" class="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4" role="dialog" aria-modal="true" aria-labelledby="rag-batch-title">
    <div class="w-full max-w-lg rounded-xl bg-white p-5 shadow-xl">
      <div class="flex items-center gap-2"><Database :size="18" class="text-violet-600" /><h3 id="rag-batch-title" class="font-medium">{{ ragBatchActionLabels[batchPreviewAction] }}</h3></div>
      <p class="mt-3 text-sm text-[var(--color-text)]">{{ ragBatchImpact(batchPreviewAction) }}</p>
      <div class="mt-3 grid grid-cols-2 gap-2 text-xs">
        <div class="rounded border border-emerald-200 bg-emerald-50 p-3"><p class="text-[var(--color-muted)]">将执行</p><strong class="mt-1 block text-lg text-emerald-700">{{ eligibleBatchDocuments.length }}</strong></div>
        <div class="rounded border border-gray-200 bg-gray-50 p-3"><p class="text-[var(--color-muted)]">状态不适用，将跳过</p><strong class="mt-1 block text-lg text-gray-600">{{ skippedBatchDocuments.length }}</strong></div>
      </div>
      <p v-if="batchPreviewAction === 'delete'" class="mt-3 rounded border border-red-200 bg-red-50 p-3 text-xs leading-5 text-red-800">
        删除不会被一次性批准。系统会按顺序为每个文档创建 L4 请求，你必须逐项确认或跳过。
      </p>
      <div class="mt-5 flex justify-end gap-2">
        <button class="rounded border px-3 py-2 text-xs" @click="batchPreviewAction = null">返回检查</button>
        <button class="rounded bg-violet-600 px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="eligibleBatchDocuments.length === 0" @click="confirmBatch">
          {{ batchPreviewAction === "delete" ? "开始逐项确认" : `确认${ragBatchActionLabels[batchPreviewAction]}` }}
        </button>
      </div>
    </div>
  </div>

  <div v-if="store.deleteRequest" class="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4" role="dialog" aria-modal="true" aria-labelledby="rag-delete-title">
    <div class="w-full max-w-lg rounded-xl bg-white p-5 shadow-xl">
      <div class="flex items-center gap-2 text-red-700"><TriangleAlert :size="18" /><h3 id="rag-delete-title" class="font-medium">确认永久删除 RAG 索引</h3></div>
      <p v-if="store.batchAction === 'delete' && store.batchRunning" class="mt-2 text-xs text-[var(--color-muted)]">
        批量删除逐项确认 {{ batchDeletePosition }}/{{ store.batchDeleteTotal }} · 当前：{{ store.currentDeleteDocument?.title }}
      </p>
      <p class="mt-3 text-sm text-[var(--color-text)]">{{ store.deleteRequest.action_summary }}</p>
      <div class="mt-3 rounded border border-red-200 bg-red-50 p-3 text-xs leading-5 text-red-800">
        <p>风险等级：L4（每次都必须确认）</p>
        <p>将删除：RAG 文档记录、作业、分块、向量和派生图片。</p>
        <p>不会删除：原始上传 Artifact，用于来源追溯与审计。</p>
        <p>该操作不可撤销；如需恢复，必须从原始文件重新入库。</p>
      </div>
      <p v-if="store.error" class="mt-3 break-words rounded border border-red-200 bg-red-50 p-2.5 text-xs text-red-700">{{ store.error }}</p>
      <div class="mt-5 flex flex-wrap justify-end gap-2">
        <button v-if="store.batchAction === 'delete' && store.batchRunning" class="rounded border px-3 py-2 text-xs" :disabled="store.mutatingDocumentId !== null" @click="store.resolveDelete('deny', true)">结束批量删除</button>
        <button class="rounded border px-3 py-2 text-xs" :disabled="store.mutatingDocumentId !== null" @click="resolveDelete('deny')">{{ store.batchAction === "delete" && store.batchRunning ? "跳过此项" : "取消删除" }}</button>
        <button class="rounded bg-red-600 px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="store.mutatingDocumentId !== null" @click="resolveDelete('allow_once')">
          {{ store.mutatingDocumentId !== null ? "删除中…" : "确认永久删除" }}
        </button>
      </div>
    </div>
  </div>
</template>
