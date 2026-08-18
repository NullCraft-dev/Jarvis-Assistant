<script setup lang="ts">
import { ref } from "vue";
import type {
  MemoryCandidateDTO, MemoryCategory, MemoryScopeType, UpdateMemoryCandidateInput, WorkspaceDTO,
} from "@jarvis/shared";

const props = defineProps<{
  items: MemoryCandidateDTO[];
  workspaces: WorkspaceDTO[];
  loading: boolean;
  saving: boolean;
}>();
const emit = defineEmits<{
  save: [candidate: MemoryCandidateDTO, input: UpdateMemoryCandidateInput];
  approve: [candidate: MemoryCandidateDTO, note: string];
  reject: [candidate: MemoryCandidateDTO, note: string];
}>();

const editingId = ref<string | null>(null);
const draftScope = ref<MemoryScopeType>("global");
const draftWorkspaceId = ref("");
const draftCategory = ref<MemoryCategory>("preference");
const draftKey = ref("");
const draftContent = ref("");
const draftImportance = ref(50);
const resolutionNotes = ref<Record<string, string>>({});
const categoryLabel: Record<MemoryCategory, string> = {
  preference: "偏好", user_fact: "用户事实", project_fact: "项目事实", rule: "规则",
};

function formatExpiry(value?: string): string {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));
}

function edit(item: MemoryCandidateDTO) {
  editingId.value = item.id;
  draftScope.value = item.scope_type;
  draftWorkspaceId.value = item.workspace_id || "";
  draftCategory.value = item.category;
  draftKey.value = item.suggested_key;
  draftContent.value = item.content;
  draftImportance.value = item.importance;
}

function save(item: MemoryCandidateDTO) {
  if (!draftKey.value.trim() || !draftContent.value.trim()) return;
  if (draftScope.value === "workspace" && !draftWorkspaceId.value) return;
  emit("save", item, {
    expected_version: item.version,
    scope_type: draftScope.value,
    ...(draftScope.value === "workspace" ? { workspace_id: draftWorkspaceId.value } : {}),
    category: draftCategory.value,
    suggested_key: draftKey.value.trim(),
    content: draftContent.value.trim(),
    importance: draftImportance.value,
  });
  editingId.value = null;
}

function resolve(item: MemoryCandidateDTO, decision: "approve" | "reject") {
  const note = (resolutionNotes.value[item.id] || "").trim();
  if (decision === "approve") emit("approve", item, note);
  else emit("reject", item, note);
}
</script>

<template>
  <section class="space-y-3">
    <div v-if="loading" class="rounded border border-[var(--color-border)] bg-white p-8 text-center text-sm text-[var(--color-muted)]">正在读取待确认记忆…</div>
    <div v-else-if="items.length === 0" class="rounded border border-dashed border-[var(--color-border)] p-8 text-center text-sm text-[var(--color-muted)]">当前没有待确认记忆。模型提取的内容只有在你批准后才会进入长期记忆。</div>
    <article v-for="item in items" :key="item.id" class="rounded-lg border border-amber-200 bg-amber-50/30 p-4">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-mono text-xs font-medium">{{ item.suggested_key }}</span>
            <span class="rounded bg-white px-2 py-0.5 text-[11px]">{{ item.scope_type === "global" ? "全局" : "工作区" }}</span>
            <span class="rounded bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700">{{ categoryLabel[item.category] }}</span>
            <span class="rounded bg-amber-100 px-2 py-0.5 text-[11px] text-amber-800">置信度 {{ Math.round(item.confidence * 100) }}%</span>
            <span v-if="item.conflict_memory_id" class="rounded bg-red-50 px-2 py-0.5 text-[11px] text-red-700">与已有记忆冲突</span>
          </div>

          <div v-if="editingId === item.id" class="mt-3 space-y-2">
            <div class="grid gap-2 sm:grid-cols-2">
              <label class="text-xs">作用域<select v-model="draftScope" class="mt-1 w-full rounded border bg-white px-2 py-1.5 text-sm"><option value="global">全局</option><option value="workspace">工作区</option></select></label>
              <label v-if="draftScope === 'workspace'" class="text-xs">工作区<select v-model="draftWorkspaceId" class="mt-1 w-full rounded border bg-white px-2 py-1.5 text-sm"><option value="">请选择</option><option v-for="workspace in props.workspaces.filter((entry) => entry.status === 'active')" :key="workspace.id" :value="workspace.id">{{ workspace.name }}</option></select></label>
              <label class="text-xs">类型<select v-model="draftCategory" class="mt-1 w-full rounded border bg-white px-2 py-1.5 text-sm"><option value="preference">偏好</option><option value="user_fact">用户事实</option><option value="project_fact">项目事实</option><option value="rule">规则</option></select></label>
              <label class="text-xs">Key<input v-model="draftKey" maxlength="128" class="mt-1 w-full rounded border px-2 py-1.5 text-sm" /></label>
            </div>
            <textarea v-model="draftContent" rows="4" maxlength="4000" class="w-full rounded border px-3 py-2 text-sm" />
            <label class="block text-xs">重要度<input v-model.number="draftImportance" type="number" min="0" max="100" class="ml-2 w-20 rounded border px-2 py-1 text-sm" /></label>
            <div class="flex gap-2"><button class="rounded bg-blue-600 px-3 py-1.5 text-xs text-white" :disabled="saving" @click="save(item)">保存修改</button><button class="rounded border bg-white px-3 py-1.5 text-xs" @click="editingId = null">取消</button></div>
          </div>
          <template v-else>
            <p class="mt-2 whitespace-pre-wrap text-sm leading-6">{{ item.content }}</p>
            <p class="mt-2 text-[11px] text-[var(--color-muted)]" :title="`Task ${item.source_task_id} · Run ${item.source_run_id}`">来源任务 {{ item.source_task_id.slice(0, 8) }} · 运行 {{ item.source_run_id.slice(0, 8) }} · 重要度 {{ item.importance }} · {{ item.extraction_policy_version }}</p>
            <p v-if="item.expires_at" class="mt-1 text-[11px] text-[var(--color-muted)]">请在 {{ formatExpiry(item.expires_at) }} 前处理，过期后系统会自动关闭候选。</p>
            <p class="mt-1 text-[11px] text-amber-800">批准后才可能进入后续任务上下文；候选本身永不注入模型。</p>
            <label class="mt-3 block text-xs text-[var(--color-muted)]">处理说明（可选）<input v-model="resolutionNotes[item.id]" maxlength="500" placeholder="例如：已核对来源与作用域" class="mt-1 w-full rounded border bg-white px-2.5 py-1.5 text-sm text-[var(--color-text)]" /></label>
          </template>
        </div>
        <div class="flex shrink-0 flex-col gap-2">
          <button class="rounded border bg-white px-2.5 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-40" :disabled="saving" @click="edit(item)">编辑</button>
          <button class="rounded bg-emerald-600 px-2.5 py-1.5 text-xs text-white disabled:opacity-40" :disabled="saving || !!item.conflict_memory_id" @click="resolve(item, 'approve')">批准并保存</button>
          <button class="rounded border border-red-200 bg-white px-2.5 py-1.5 text-xs text-red-600 disabled:opacity-40" :disabled="saving" @click="resolve(item, 'reject')">拒绝</button>
        </div>
      </div>
    </article>
  </section>
</template>
