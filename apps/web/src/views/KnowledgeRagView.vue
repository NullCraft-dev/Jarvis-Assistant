<script setup lang="ts">
import { computed, onMounted, watch } from "vue";
import { useRoute } from "vue-router";
import { useRagDocumentStore } from "@/stores/ragDocumentStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import RagDocumentLibrary from "@/features/knowledge/components/RagDocumentLibrary.vue";

const rag = useRagDocumentStore();
const workspaces = useWorkspaceStore();
const route = useRoute();
const focusedDocumentId = computed(() => {
  const value = route.query.document_id;
  return typeof value === "string" && /^[0-9a-f-]{36}$/i.test(value) ? value : "";
});
const focusedChunkId = computed(() => {
  const value = route.query.chunk_id;
  return typeof value === "string" && /^[0-9a-f-]{36}$/i.test(value) ? value : "";
});

onMounted(async () => {
  await workspaces.loadWorkspaces();
  await rag.load(workspaces.selectedWorkspaceId);
});
watch(() => workspaces.selectedWorkspaceId, (workspaceId) => rag.load(workspaceId));
</script>

<template>
  <main class="min-h-0 flex-1 overflow-auto">
    <div class="mx-auto max-w-6xl space-y-5 p-4 sm:p-5">
      <section>
        <h2 class="text-lg font-medium">RAG 文档库</h2>
        <p class="mt-1 text-xs leading-5 text-[var(--color-muted)]">管理模型检索使用的 PDF、索引生命周期和受控批量运维。</p>
      </section>
      <RagDocumentLibrary :focused-document-id="focusedDocumentId" :focused-chunk-id="focusedChunkId" />
    </div>
  </main>
</template>
