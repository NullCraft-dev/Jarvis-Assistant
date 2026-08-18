<script setup lang="ts">
import { onMounted } from "vue";
import { Brain, AlertCircle, RefreshCw } from "@lucide/vue";
import type { CreateMemoryInput, MemoryDTO } from "@jarvis/shared";
import { useMemoryStore } from "@/stores/memoryStore";
import MemoryEditor from "@/features/memory/components/MemoryEditor.vue";
import MemoryList from "@/features/memory/components/MemoryList.vue";
import MemoryCandidateList from "@/features/memory/components/MemoryCandidateList.vue";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const store = useMemoryStore();
const workspaceStore = useWorkspaceStore();
async function refresh() { await Promise.all([store.load(), workspaceStore.loadWorkspaces()]); }
onMounted(refresh);
async function create(input: CreateMemoryInput) { await store.create(input); }
async function toggle(item: MemoryDTO) { await store.update(item.id, { expected_version: item.version, status: item.status === "active" ? "disabled" : "active" }); }
async function save(item: MemoryDTO, content: string, importance: number) { await store.update(item.id, { expected_version: item.version, content, importance }); }
async function remove(item: MemoryDTO) {
  if (window.confirm(`确定永久删除记忆“${item.key}”吗？此操作不可恢复。`)) await store.remove(item.id);
}
async function saveCandidate(item: import("@jarvis/shared").MemoryCandidateDTO, input: import("@jarvis/shared").UpdateMemoryCandidateInput) { await store.updateCandidate(item.id, input); }
async function approveCandidate(item: import("@jarvis/shared").MemoryCandidateDTO, note: string) { await store.approveCandidate(item.id, { expected_version: item.version, note }); }
async function rejectCandidate(item: import("@jarvis/shared").MemoryCandidateDTO, note: string) { await store.rejectCandidate(item.id, { expected_version: item.version, note }); }
</script>

<template>
  <div class="h-full overflow-auto">
    <header class="border-b border-[var(--color-border)] px-5 py-4"><div class="flex items-center justify-between gap-3"><div class="flex items-center gap-2"><Brain :size="18" class="text-[var(--color-muted)]" /><h1 class="font-medium">长期记忆</h1></div><button class="flex items-center gap-1 rounded border bg-white px-2.5 py-1.5 text-xs disabled:opacity-40" :disabled="store.loading" @click="refresh"><RefreshCw :size="13" :class="store.loading ? 'animate-spin' : ''" />刷新</button></div><p class="mt-1 text-xs text-[var(--color-muted)]">管理跨会话复用的全局与工作区背景。记忆不能覆盖系统安全和权限规则。</p></header>
    <main class="mx-auto max-w-4xl space-y-6 p-5">
      <div v-if="store.error" class="flex items-center gap-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600"><AlertCircle :size="14" />{{ store.error }}</div>
      <section><h2 class="mb-2 text-sm font-medium">待确认记忆 <span class="text-xs font-normal text-[var(--color-muted)]">{{ store.candidates.length }}</span></h2><MemoryCandidateList :items="store.candidates" :workspaces="workspaceStore.workspaces" :loading="store.loading" :saving="store.saving" @save="saveCandidate" @approve="approveCandidate" @reject="rejectCandidate" /></section>
      <section class="space-y-3"><h2 class="text-sm font-medium">已保存记忆</h2><MemoryEditor :saving="store.saving" @create="create" /><MemoryList :items="store.memories" :loading="store.loading" :saving="store.saving" @save="save" @toggle="toggle" @remove="remove" /></section>
    </main>
  </div>
</template>
