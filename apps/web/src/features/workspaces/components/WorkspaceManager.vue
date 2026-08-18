<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { FolderPlus, Folder, RefreshCw, Trash2, ShieldCheck, AlertCircle } from "@lucide/vue";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const workspaceStore = useWorkspaceStore();
const confirmingId = ref<string | null>(null);
const revokingId = ref<string | null>(null);
const picking = ref(false);

const orderedWorkspaces = computed(() =>
  [...workspaceStore.activeWorkspaces].sort((a, b) => {
    if (a.source !== b.source) return a.source === "configured" ? -1 : 1;
    return a.name.localeCompare(b.name);
  })
);

async function pickWorkspace() {
  if (picking.value) return;
  picking.value = true;
  try {
    await workspaceStore.pickAndAddWorkspace();
  } finally {
    picking.value = false;
  }
}

async function confirmRevoke(workspaceId: string) {
  revokingId.value = workspaceId;
  try {
    if (await workspaceStore.revokeWorkspace(workspaceId)) {
      confirmingId.value = null;
    }
  } finally {
    revokingId.value = null;
  }
}

onMounted(() => workspaceStore.loadWorkspaces());
</script>

<template>
  <section class="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]">
    <div class="flex items-center justify-between gap-4 border-b border-[var(--color-border)] px-4 py-3">
      <div>
        <h2 class="text-sm font-medium text-[var(--color-text)]">工作区</h2>
        <p class="mt-0.5 text-xs text-[var(--color-muted)]">
          Agent 只能在当前任务绑定的工作区范围内访问文件。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button
          class="inline-flex items-center gap-1 rounded border border-[var(--color-border)] px-2.5 py-1.5 text-xs hover:bg-gray-50 disabled:opacity-50"
          :disabled="workspaceStore.loading"
          @click="workspaceStore.loadWorkspaces()"
        >
          <RefreshCw :size="13" :class="{ 'animate-spin': workspaceStore.loading }" />
          刷新
        </button>
        <button
          class="inline-flex items-center gap-1 rounded bg-[var(--color-accent)] px-2.5 py-1.5 text-xs text-white hover:opacity-90 disabled:opacity-50"
          :disabled="picking"
          @click="pickWorkspace"
        >
          <FolderPlus :size="13" />
          {{ picking ? "选择中…" : "添加工作区" }}
        </button>
      </div>
    </div>

    <div v-if="workspaceStore.error" class="flex items-center gap-2 border-b border-red-100 bg-red-50 px-4 py-2 text-xs text-red-600">
      <AlertCircle :size="14" />
      {{ workspaceStore.error }}
    </div>

    <div v-if="orderedWorkspaces.length === 0 && !workspaceStore.loading" class="px-4 py-10 text-center">
      <Folder :size="28" class="mx-auto text-[var(--color-muted)]" />
      <p class="mt-2 text-sm text-[var(--color-muted)]">尚未注册工作区</p>
      <p class="mt-1 text-xs text-[var(--color-border)]">添加一个明确的项目目录后再创建文件任务。</p>
    </div>

    <div v-else class="divide-y divide-[var(--color-border)]">
      <div v-for="ws in orderedWorkspaces" :key="ws.id" class="flex items-center gap-3 px-4 py-3">
        <Folder :size="18" class="shrink-0 text-blue-500" />
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <span class="truncate text-sm font-medium text-[var(--color-text)]">{{ ws.name }}</span>
            <span v-if="ws.id === workspaceStore.selectedWorkspaceId" class="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] text-blue-600">当前</span>
            <span v-if="ws.source === 'configured'" class="inline-flex items-center gap-1 rounded bg-green-50 px-1.5 py-0.5 text-[10px] text-green-700">
              <ShieldCheck :size="10" /> 配置管理
            </span>
          </div>
          <div class="mt-0.5 truncate font-mono text-xs text-[var(--color-muted)]" :title="ws.canonical_path">
            {{ ws.canonical_path }}
          </div>
        </div>

        <div v-if="ws.source === 'user_picker'" class="flex shrink-0 items-center gap-1">
          <template v-if="confirmingId === ws.id">
            <button class="rounded px-2 py-1 text-xs text-[var(--color-muted)] hover:bg-gray-50" @click="confirmingId = null">取消</button>
            <button
              class="rounded bg-red-600 px-2 py-1 text-xs text-white disabled:opacity-50"
              :disabled="revokingId === ws.id"
              @click="confirmRevoke(ws.id)"
            >
              {{ revokingId === ws.id ? "撤销中…" : "确认撤销" }}
            </button>
          </template>
          <button
            v-else
            class="inline-flex items-center gap-1 rounded px-2 py-1 text-xs text-red-600 hover:bg-red-50"
            @click="confirmingId = ws.id"
          >
            <Trash2 :size="12" /> 撤销
          </button>
        </div>
      </div>
    </div>
  </section>
</template>
