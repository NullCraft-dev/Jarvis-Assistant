<script setup lang="ts">
import { ref } from "vue";
import type { MemoryDTO } from "@jarvis/shared";
defineProps<{ items: MemoryDTO[]; loading: boolean; saving: boolean }>();
const emit = defineEmits<{
  toggle: [memory: MemoryDTO];
  remove: [memory: MemoryDTO];
  save: [memory: MemoryDTO, content: string, importance: number];
}>();
const categoryLabel: Record<string, string> = { preference: "偏好", user_fact: "用户事实", project_fact: "项目事实", rule: "规则" };
const editingId = ref<string | null>(null);
const draftContent = ref("");
const draftImportance = ref(50);
function edit(item: MemoryDTO) {
  editingId.value = item.id; draftContent.value = item.content; draftImportance.value = item.importance;
}
function save(item: MemoryDTO) {
  if (!draftContent.value.trim()) return;
  emit("save", item, draftContent.value.trim(), draftImportance.value);
  editingId.value = null;
}
</script>

<template>
  <section class="space-y-3">
    <div v-if="loading" class="rounded border border-[var(--color-border)] bg-white p-8 text-center text-sm text-[var(--color-muted)]">正在读取长期记忆…</div>
    <div v-else-if="items.length === 0" class="rounded border border-dashed border-[var(--color-border)] p-8 text-center text-sm text-[var(--color-muted)]">还没有长期记忆。添加后，它会在符合范围的后续任务中作为背景上下文使用。</div>
    <article v-for="item in items" :key="item.id" class="rounded-lg border border-[var(--color-border)] bg-white p-4" :class="{ 'opacity-60': item.status === 'disabled' }">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0 flex-1"><div class="flex flex-wrap items-center gap-2"><span class="font-mono text-xs font-medium">{{ item.key }}</span><span class="rounded bg-gray-100 px-2 py-0.5 text-[11px]">{{ item.scope_type === "global" ? "全局" : "工作区" }}</span><span class="rounded bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700">{{ categoryLabel[item.category] }}</span></div>
          <div v-if="editingId === item.id" class="mt-3 space-y-2"><textarea v-model="draftContent" aria-label="编辑记忆内容" rows="3" maxlength="4000" class="w-full rounded border px-3 py-2 text-sm" /><label class="block text-xs text-[var(--color-muted)]">重要度<input v-model.number="draftImportance" aria-label="编辑重要度" type="number" min="0" max="100" class="ml-2 w-20 rounded border px-2 py-1 text-sm" /></label><div class="flex gap-2"><button class="rounded bg-blue-600 px-3 py-1.5 text-xs text-white" :disabled="saving" @click="save(item)">保存修改</button><button class="rounded border px-3 py-1.5 text-xs" @click="editingId = null">取消</button></div></div>
          <template v-else><p class="mt-2 whitespace-pre-wrap text-sm leading-6">{{ item.content }}</p><p class="mt-2 text-[11px] text-[var(--color-muted)]">重要度 {{ item.importance }} · {{ item.status === "active" ? "会注入上下文" : "已停用" }} · v{{ item.version }}</p></template>
        </div>
        <div class="flex shrink-0 gap-2"><button class="rounded border px-2.5 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-40" :disabled="saving" @click="edit(item)">编辑</button><button class="rounded border px-2.5 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-40" :disabled="saving" @click="emit('toggle', item)">{{ item.status === "active" ? "停用" : "启用" }}</button><button class="rounded border border-red-200 px-2.5 py-1.5 text-xs text-red-600 hover:bg-red-50 disabled:opacity-40" :disabled="saving" @click="emit('remove', item)">删除</button></div>
      </div>
    </article>
  </section>
</template>
