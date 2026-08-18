<script setup lang="ts">
import { ref, watch } from "vue";
import type { ArtifactDTO } from "@jarvis/shared";
import { FileText } from "@lucide/vue";
import { getArtifact } from "@/api/client";

const props = defineProps<{ artifact: ArtifactDTO }>();
const content = ref(props.artifact.content ?? "");
const loading = ref(false);
const error = ref("");
const loaded = ref(Boolean(props.artifact.content));
const isFileDeliverable = () => props.artifact.kind === "file";
const workspacePath = () => {
  const value = props.artifact.metadata?.workspace_relative_path;
  return typeof value === "string" ? value : "";
};

function formatBytes(value?: number) {
  if (typeof value !== "number") return "";
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KiB`;
}

watch(
  () => props.artifact,
  (artifact) => {
    content.value = artifact.content ?? "";
    loaded.value = Boolean(artifact.content);
    error.value = "";
  },
);

async function handleToggle(event: Event) {
  const details = event.currentTarget as HTMLDetailsElement;
  if (!details.open || loaded.value || loading.value) return;
  loading.value = true;
  error.value = "";
  const result = await getArtifact(props.artifact.id);
  if (result.ok) {
    content.value = result.data.content ?? "";
    loaded.value = true;
  } else {
    error.value = result.error.message;
  }
  loading.value = false;
}
</script>

<template>
  <details
    class="min-w-0 max-w-full overflow-hidden rounded-lg border border-[var(--color-border)] bg-white px-3 py-2"
    @toggle="handleToggle"
  >
    <summary class="flex min-w-0 cursor-pointer list-none items-center gap-2 text-sm text-gray-700">
      <FileText :size="15" class="shrink-0 text-sky-600" />
      <span class="min-w-0 flex-1 truncate font-medium" :title="artifact.title">{{ artifact.title }}</span>
      <span class="ml-auto hidden shrink-0 text-xs text-gray-400 sm:inline">交付物 · {{ artifact.kind }}</span>
    </summary>
    <p v-if="isFileDeliverable() && workspacePath()" class="mt-2 break-all text-xs text-gray-600">
      工作区路径：{{ workspacePath() }}
    </p>
    <pre
      v-if="content"
      class="mt-2 max-h-64 max-w-full overflow-auto whitespace-pre-wrap break-words rounded bg-gray-50 p-2 text-xs text-gray-700 [overflow-wrap:anywhere]"
    >{{ content }}</pre>
    <p v-else-if="loading" class="mt-2 text-xs text-gray-500">正在读取产物…</p>
    <p v-else-if="error" class="mt-2 break-words text-xs text-red-600 [overflow-wrap:anywhere]">{{ error }}</p>
    <p v-else class="mt-2 text-xs text-gray-500">展开后按需读取产物内容。</p>
    <p v-if="isFileDeliverable()" class="mt-1 text-xs text-gray-400">
      {{ formatBytes(artifact.file_size_bytes) }}
      <span v-if="artifact.mime_type"> · {{ artifact.mime_type }}</span>
    </p>
  </details>
</template>
