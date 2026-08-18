<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { FilePlus2, FileText, FolderOpen, LoaderCircle, RefreshCw, Search, X } from "@lucide/vue";
import { useRoute, useRouter } from "vue-router";
import type { KnowledgeDocumentKind } from "@jarvis/shared";
import { useKnowledgeStore } from "@/stores/knowledgeStore";

const store = useKnowledgeStore();
const route = useRoute();
const router = useRouter();
const query = ref("");
const selectedKind = ref<KnowledgeDocumentKind | "all">("all");
const showCreate = ref(false);
const title = ref("");
const content = ref("");
const tags = ref("");
const kind = ref<KnowledgeDocumentKind>("note");
const canSwitchVault = computed(() => Boolean(
  store.vaults[0]
  && store.suggestedPath
  && store.vaults[0].canonical_path !== store.suggestedPath,
));

const filteredDocuments = computed(() => {
  const normalizedQuery = query.value.trim().toLocaleLowerCase();
  return store.documents.filter((document) => {
    if (selectedKind.value !== "all" && document.kind !== selectedKind.value) return false;
    if (!normalizedQuery) return true;
    return `${document.title} ${document.relative_path}`.toLocaleLowerCase().includes(normalizedQuery);
  });
});

function syncCreateQuery() {
  if (route.query.create === "1") showCreate.value = true;
}

function closeCreate() {
  if (store.saving) return;
  showCreate.value = false;
  if (route.query.create === "1") router.replace({ path: "/knowledge/documents" });
}

async function submit() {
  const ok = await store.create({
    title: title.value,
    kind: kind.value,
    content: content.value,
    tags: tags.value.split(",").map((value) => value.trim()).filter(Boolean),
    source_urls: [],
  });
  if (!ok) return;
  title.value = "";
  content.value = "";
  tags.value = "";
  kind.value = "note";
  closeCreate();
}

onMounted(async () => {
  syncCreateQuery();
  await store.load();
});
watch(() => route.query.create, syncCreateQuery);
</script>

<template>
  <main class="min-h-0 flex-1 overflow-auto">
    <div class="mx-auto max-w-6xl space-y-5 p-4 sm:p-5">
      <section class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="text-lg font-medium">知识文档</h2>
          <p class="mt-1 text-xs leading-5 text-[var(--color-muted)]">管理 Jarvis 专用 Obsidian Vault 中的人类可读 Markdown。</p>
        </div>
        <div class="flex gap-2">
          <button class="flex items-center gap-1.5 rounded border bg-white px-3 py-2 text-xs disabled:opacity-50" :disabled="store.loading" @click="store.load">
            <LoaderCircle v-if="store.loading" :size="13" class="animate-spin" /><RefreshCw v-else :size="13" />刷新
          </button>
          <button v-if="store.vaults.length" class="flex items-center gap-1.5 rounded bg-blue-600 px-3 py-2 text-xs text-white" @click="showCreate = true">
            <FilePlus2 :size="13" />新建文档
          </button>
        </div>
      </section>

      <p v-if="store.error" class="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">{{ store.error }}</p>

      <section v-if="!store.vaults.length" class="rounded-xl border bg-white p-6">
        <div class="flex items-center gap-2 text-sm font-medium"><FolderOpen :size="17" class="text-blue-500" />连接 Jarvis 专用 Vault</div>
        <p class="mt-2 text-xs leading-5 text-[var(--color-muted)]">Jarvis 只访问自己的独立 Vault，不会读取已有个人 Vault。</p>
        <p class="mt-3 break-all rounded bg-gray-50 p-3 font-mono text-xs text-[var(--color-muted)]">{{ store.suggestedPath || "正在获取建议路径…" }}</p>
        <button class="mt-4 rounded bg-blue-600 px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="store.saving || !store.suggestedPath" @click="store.connect">
          {{ store.saving ? "连接中…" : "连接并初始化" }}
        </button>
      </section>

      <template v-else>
        <section class="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-white p-4">
          <div class="min-w-0">
            <div class="flex items-center gap-2 text-sm font-medium"><FolderOpen :size="15" class="text-blue-500" />{{ store.vaults[0].name }}</div>
            <p class="mt-1 break-all text-xs text-[var(--color-muted)]">{{ store.vaults[0].canonical_path }}</p>
          </div>
          <span class="rounded-full bg-emerald-50 px-2.5 py-1 text-[11px] text-emerald-700">已连接</span>
        </section>

        <section v-if="canSwitchVault" class="rounded-xl border border-blue-200 bg-blue-50 p-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="min-w-0">
              <p class="text-sm font-medium text-blue-950">可切换到当前配置的 Jarvis Vault</p>
              <p class="mt-1 break-all font-mono text-xs text-blue-800">{{ store.suggestedPath }}</p>
              <p class="mt-2 text-xs leading-5 text-blue-900">切换只会停用当前连接，不会删除原 Vault 中的文件或索引。</p>
            </div>
            <button class="rounded bg-blue-600 px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="store.saving" @click="store.connect">
              {{ store.saving ? "切换中…" : "切换到此 Vault" }}
            </button>
          </div>
        </section>

        <section class="space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <h3 class="text-sm font-medium">全部文档 <span class="font-normal text-[var(--color-muted)]">{{ filteredDocuments.length }}/{{ store.documents.length }}</span></h3>
            <div class="flex w-full flex-wrap gap-2 sm:w-auto">
              <label class="flex min-w-52 flex-1 items-center gap-2 rounded border bg-white px-3 py-2 sm:flex-none">
                <Search :size="13" class="text-[var(--color-muted)]" />
                <input v-model="query" class="min-w-0 flex-1 bg-transparent text-xs outline-none" placeholder="搜索标题或路径" />
              </label>
              <select v-model="selectedKind" class="rounded border bg-white px-3 py-2 text-xs">
                <option value="all">全部类型</option><option value="note">笔记</option><option value="report">报告</option><option value="source">来源说明</option>
              </select>
            </div>
          </div>

          <div v-if="store.loading && !store.documents.length" class="flex items-center justify-center gap-2 rounded-xl border border-dashed p-8 text-xs text-[var(--color-muted)]">
            <LoaderCircle :size="14" class="animate-spin" />正在读取知识文档…
          </div>
          <div v-else-if="!filteredDocuments.length" class="rounded-xl border border-dashed p-8 text-center text-xs text-[var(--color-muted)]">
            {{ store.documents.length ? "没有符合筛选条件的文档。" : "暂无文档，可以从新建一篇笔记开始。" }}
          </div>
          <div v-else class="grid gap-3 md:grid-cols-2">
            <article v-for="document in filteredDocuments" :key="document.id" class="flex min-w-0 items-start gap-3 rounded-xl border bg-white p-4">
              <span class="rounded-lg bg-blue-50 p-2 text-blue-500"><FileText :size="16" /></span>
              <div class="min-w-0">
                <p class="truncate text-sm font-medium" :title="document.title">{{ document.title }}</p>
                <p class="mt-1 break-all text-xs text-[var(--color-muted)]">{{ document.relative_path }}</p>
                <span class="mt-3 inline-block rounded bg-gray-100 px-2 py-1 text-[10px] text-gray-600">{{ document.kind === "note" ? "笔记" : document.kind === "report" ? "报告" : "来源说明" }}</span>
              </div>
            </article>
          </div>
        </section>
      </template>
    </div>
  </main>

  <div v-if="showCreate" class="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4" role="dialog" aria-modal="true" aria-labelledby="knowledge-create-title" @click.self="closeCreate">
    <section class="flex max-h-[calc(100vh-2rem)] w-full max-w-2xl flex-col overflow-hidden rounded-xl bg-white shadow-xl">
      <header class="flex items-center justify-between gap-3 border-b px-5 py-4">
        <div><h3 id="knowledge-create-title" class="font-medium">新建知识文档</h3><p class="mt-1 text-xs text-[var(--color-muted)]">保存到 Jarvis 专用 Obsidian Vault。</p></div>
        <button class="rounded p-1.5 text-[var(--color-muted)] hover:bg-gray-100" :disabled="store.saving" aria-label="关闭" @click="closeCreate"><X :size="17" /></button>
      </header>
      <div class="min-h-0 flex-1 space-y-3 overflow-auto p-5">
        <div class="grid gap-3 sm:grid-cols-3">
          <input v-model="title" class="rounded border px-3 py-2 text-sm sm:col-span-2" maxlength="500" placeholder="标题" autofocus />
          <select v-model="kind" class="rounded border px-3 py-2 text-sm"><option value="note">笔记</option><option value="report">报告</option><option value="source">来源说明</option></select>
        </div>
        <textarea v-model="content" class="min-h-64 w-full rounded border px-3 py-2 text-sm" maxlength="524288" placeholder="Markdown 正文"></textarea>
        <input v-model="tags" class="w-full rounded border px-3 py-2 text-sm" placeholder="标签，以逗号分隔" />
        <p v-if="store.error" class="rounded border border-red-200 bg-red-50 p-3 text-xs text-red-700">{{ store.error }}</p>
      </div>
      <footer class="flex justify-end gap-2 border-t px-5 py-4">
        <button class="rounded border px-3 py-2 text-xs" :disabled="store.saving" @click="closeCreate">取消</button>
        <button class="rounded bg-blue-600 px-4 py-2 text-xs text-white disabled:opacity-50" :disabled="store.saving || !title.trim() || !content.trim()" @click="submit">{{ store.saving ? "保存中…" : "保存到 Obsidian" }}</button>
      </footer>
    </section>
  </div>
</template>
