<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import type { CreateMemoryInput, MemoryCategory, MemoryScopeType, WorkspaceDTO } from "@jarvis/shared";
import { listWorkspaces } from "@/api/client";

const props = defineProps<{ saving: boolean }>();
const emit = defineEmits<{ create: [input: CreateMemoryInput] }>();
const scope = ref<MemoryScopeType>("global");
const category = ref<MemoryCategory>("preference");
const workspaceId = ref("");
const key = ref("");
const content = ref("");
const importance = ref(50);
const workspaces = ref<WorkspaceDTO[]>([]);
const valid = computed(() => key.value.trim() && content.value.trim() && (scope.value === "global" || workspaceId.value));

onMounted(async () => {
  const result = await listWorkspaces();
  if (result.ok) workspaces.value = result.data.workspaces;
});

function submit() {
  if (!valid.value) return;
  emit("create", {
    scope_type: scope.value,
    workspace_id: scope.value === "workspace" ? workspaceId.value : undefined,
    category: category.value, key: key.value.trim().toLowerCase(),
    content: content.value.trim(), importance: importance.value,
  });
}
</script>

<template>
  <section class="rounded-lg border border-[var(--color-border)] bg-white p-4">
    <h2 class="text-sm font-medium">新增长期记忆</h2>
    <p class="mt-1 text-xs text-[var(--color-muted)]">你可以直接新增记忆；任务结束后提取的候选也必须经你批准才会保存。</p>
    <div class="mt-4 grid gap-3 md:grid-cols-2">
      <label class="text-xs text-[var(--color-muted)]">作用域<select v-model="scope" class="mt-1 w-full rounded border px-3 py-2 text-sm text-[var(--color-text)]"><option value="global">全局</option><option value="workspace">工作区</option></select></label>
      <label class="text-xs text-[var(--color-muted)]">分类<select v-model="category" class="mt-1 w-full rounded border px-3 py-2 text-sm text-[var(--color-text)]"><option value="preference">偏好</option><option value="user_fact">用户事实</option><option value="project_fact">项目事实</option><option value="rule">规则</option></select></label>
      <label v-if="scope === 'workspace'" class="text-xs text-[var(--color-muted)]">工作区<select v-model="workspaceId" class="mt-1 w-full rounded border px-3 py-2 text-sm text-[var(--color-text)]"><option value="">请选择</option><option v-for="ws in workspaces" :key="ws.id" :value="ws.id">{{ ws.name }}</option></select></label>
      <label class="text-xs text-[var(--color-muted)]">唯一键<input v-model="key" placeholder="response.language" class="mt-1 w-full rounded border px-3 py-2 text-sm text-[var(--color-text)]" /></label>
      <label class="text-xs text-[var(--color-muted)]">重要度（0–100）<input v-model.number="importance" type="number" min="0" max="100" class="mt-1 w-full rounded border px-3 py-2 text-sm text-[var(--color-text)]" /></label>
      <label class="text-xs text-[var(--color-muted)] md:col-span-2">内容<textarea v-model="content" rows="3" maxlength="4000" placeholder="例如：默认使用中文回答。" class="mt-1 w-full resize-y rounded border px-3 py-2 text-sm text-[var(--color-text)]" /></label>
    </div>
    <div class="mt-3 flex justify-end"><button :disabled="!valid || props.saving" class="rounded bg-blue-600 px-4 py-2 text-xs text-white disabled:opacity-40" @click="submit">{{ props.saving ? "保存中…" : "保存记忆" }}</button></div>
  </section>
</template>
