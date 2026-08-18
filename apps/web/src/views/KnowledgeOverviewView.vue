<script setup lang="ts">
import { computed, onMounted, watch } from "vue";
import {
  ArrowRight,
  BookOpenText,
  CircleAlert,
  Database,
  FilePlus2,
  FileText,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from "@lucide/vue";
import { useKnowledgeStore } from "@/stores/knowledgeStore";
import { useRagDocumentStore } from "@/stores/ragDocumentStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const knowledge = useKnowledgeStore();
const rag = useRagDocumentStore();
const workspaces = useWorkspaceStore();

const readyCount = computed(() => rag.documents.filter((document) => document.status === "ready").length);
const indexingCount = computed(() => rag.documents.filter((document) => document.status === "indexing").length);
const attentionCount = computed(() => rag.documents.filter((document) =>
  document.status === "failed" || document.index_state === "stale",
).length);
const recentKnowledgeDocuments = computed(() => knowledge.documents.slice(0, 4));
const recentRagDocuments = computed(() => [...rag.documents]
  .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
  .slice(0, 4));
const loading = computed(() => knowledge.loading || workspaces.loading || rag.loading);

async function loadOverview() {
  await Promise.all([knowledge.load(), workspaces.loadWorkspaces()]);
  await rag.load(workspaces.selectedWorkspaceId);
}

onMounted(loadOverview);
watch(() => workspaces.selectedWorkspaceId, (workspaceId) => rag.load(workspaceId));
</script>

<template>
  <main class="min-h-0 flex-1 overflow-auto">
    <div class="mx-auto max-w-6xl space-y-6 p-4 sm:p-5">
      <section class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="text-lg font-medium">知识工作台</h2>
          <p class="mt-1 text-xs leading-5 text-[var(--color-muted)]">
            查看知识资产的整体状态，再进入对应空间完成创建、入库或质量审核。
          </p>
        </div>
        <button
          class="flex items-center gap-1.5 rounded border bg-white px-3 py-2 text-xs disabled:opacity-50"
          :disabled="loading"
          @click="loadOverview"
        >
          <LoaderCircle v-if="loading" :size="13" class="animate-spin" />
          <RefreshCw v-else :size="13" />
          刷新概览
        </button>
      </section>

      <div v-if="knowledge.error || rag.error" class="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
        {{ knowledge.error || rag.error }}
      </div>

      <section class="grid gap-4 lg:grid-cols-3">
        <RouterLink to="/knowledge/documents" class="group rounded-xl border bg-white p-5 transition hover:-translate-y-0.5 hover:border-blue-200 hover:shadow-sm">
          <div class="flex items-start justify-between gap-3">
            <span class="rounded-lg bg-blue-50 p-2.5 text-blue-600"><FileText :size="19" /></span>
            <ArrowRight :size="16" class="text-gray-300 transition group-hover:translate-x-0.5 group-hover:text-blue-500" />
          </div>
          <p class="mt-5 text-sm font-medium">知识文档</p>
          <p class="mt-1 text-2xl font-semibold tracking-tight">{{ knowledge.documents.length }}</p>
          <p class="mt-2 text-xs leading-5 text-[var(--color-muted)]">
            {{ knowledge.vaults.length ? `${knowledge.vaults[0].name} 已连接` : "尚未连接 Jarvis Vault" }}
          </p>
        </RouterLink>

        <RouterLink to="/knowledge/rag" class="group rounded-xl border bg-white p-5 transition hover:-translate-y-0.5 hover:border-violet-200 hover:shadow-sm">
          <div class="flex items-start justify-between gap-3">
            <span class="rounded-lg bg-violet-50 p-2.5 text-violet-600"><Database :size="19" /></span>
            <ArrowRight :size="16" class="text-gray-300 transition group-hover:translate-x-0.5 group-hover:text-violet-500" />
          </div>
          <p class="mt-5 text-sm font-medium">RAG 文档库</p>
          <p class="mt-1 text-2xl font-semibold tracking-tight">{{ rag.documents.length }}</p>
          <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-[var(--color-muted)]">
            <span>{{ readyCount }} 可检索</span>
            <span v-if="indexingCount">{{ indexingCount }} 处理中</span>
            <span v-if="attentionCount" class="text-amber-700">{{ attentionCount }} 需关注</span>
          </div>
        </RouterLink>

        <RouterLink to="/knowledge/quality" class="group rounded-xl border bg-white p-5 transition hover:-translate-y-0.5 hover:border-emerald-200 hover:shadow-sm">
          <div class="flex items-start justify-between gap-3">
            <span class="rounded-lg bg-emerald-50 p-2.5 text-emerald-600"><ShieldCheck :size="19" /></span>
            <ArrowRight :size="16" class="text-gray-300 transition group-hover:translate-x-0.5 group-hover:text-emerald-500" />
          </div>
          <p class="mt-5 text-sm font-medium">RAG 质量中心</p>
          <p class="mt-1 text-2xl font-semibold tracking-tight">人工审核</p>
          <p class="mt-2 text-xs leading-5 text-[var(--color-muted)]">诊断用户反馈、复核检索证据并生成脱敏回归候选。</p>
        </RouterLink>
      </section>

      <section class="grid gap-4 lg:grid-cols-[1fr_1fr]">
        <div class="rounded-xl border bg-white p-4">
          <div class="flex items-center justify-between gap-3">
            <div class="flex items-center gap-2 text-sm font-medium"><BookOpenText :size="16" class="text-blue-500" />最近知识文档</div>
            <RouterLink to="/knowledge/documents" class="text-xs text-blue-600 hover:text-blue-700">查看全部</RouterLink>
          </div>
          <div v-if="!knowledge.vaults.length" class="mt-4 rounded-lg border border-dashed p-5 text-center text-xs text-[var(--color-muted)]">
            连接 Jarvis Vault 后，可以在这里管理 Markdown 知识。
          </div>
          <div v-else-if="!recentKnowledgeDocuments.length" class="mt-4 rounded-lg border border-dashed p-5 text-center text-xs text-[var(--color-muted)]">
            暂无知识文档。
          </div>
          <div v-else class="mt-3 divide-y">
            <div v-for="document in recentKnowledgeDocuments" :key="document.id" class="flex min-w-0 items-center gap-3 py-3">
              <FileText :size="15" class="shrink-0 text-blue-500" />
              <div class="min-w-0">
                <p class="truncate text-sm">{{ document.title }}</p>
                <p class="mt-0.5 truncate text-xs text-[var(--color-muted)]">{{ document.relative_path }}</p>
              </div>
            </div>
          </div>
        </div>

        <div class="rounded-xl border bg-white p-4">
          <div class="flex items-center justify-between gap-3">
            <div class="flex items-center gap-2 text-sm font-medium"><Sparkles :size="16" class="text-violet-500" />最近 RAG 活动</div>
            <RouterLink to="/knowledge/rag" class="text-xs text-violet-600 hover:text-violet-700">管理文档</RouterLink>
          </div>
          <div v-if="!workspaces.selectedWorkspaceId" class="mt-4 rounded-lg border border-dashed p-5 text-center text-xs text-[var(--color-muted)]">
            请先在顶部选择工作区。
          </div>
          <div v-else-if="!recentRagDocuments.length" class="mt-4 rounded-lg border border-dashed p-5 text-center text-xs text-[var(--color-muted)]">
            当前工作区暂无 RAG 文档。
          </div>
          <div v-else class="mt-3 divide-y">
            <div v-for="document in recentRagDocuments" :key="document.id" class="flex min-w-0 items-center gap-3 py-3">
              <CircleAlert v-if="document.status === 'failed' || document.index_state === 'stale'" :size="15" class="shrink-0 text-amber-500" />
              <Database v-else :size="15" class="shrink-0 text-violet-500" />
              <div class="min-w-0 flex-1">
                <p class="truncate text-sm">{{ document.title }}</p>
                <p class="mt-0.5 text-xs text-[var(--color-muted)]">{{ document.status === "ready" ? "可检索" : document.status === "indexing" ? "处理中" : document.status === "failed" ? "处理失败" : "已停用" }} · {{ document.chunk_count }} 个分块</p>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section class="flex flex-wrap gap-2 rounded-xl border border-blue-100 bg-blue-50/60 p-4">
        <p class="mr-auto min-w-60 text-xs leading-5 text-blue-900">知识文档与 RAG 文档保持独立生命周期；保存 Markdown 不会自动启动向量化。</p>
        <RouterLink to="/knowledge/documents?create=1" class="flex items-center gap-1.5 rounded bg-blue-600 px-3 py-2 text-xs text-white"><FilePlus2 :size="13" />新建知识文档</RouterLink>
        <RouterLink to="/knowledge/rag" class="flex items-center gap-1.5 rounded border border-blue-200 bg-white px-3 py-2 text-xs text-blue-700"><Database :size="13" />添加 RAG 文档</RouterLink>
      </section>
    </div>
  </main>
</template>
