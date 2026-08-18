<script setup lang="ts">
// 任务列表视图 — MVP 阶段用于观察 storage 中的任务并恢复当前任务上下文
// 真源：docs/11-frontend-app-ui-design.md § Task Dashboard, docs/13-interface-contract.md

import { useTaskStore } from "@/stores/taskStore";
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  ListTodo,
  Clock,
  ArrowRight,
  Loader2,
  AlertCircle,
  Inbox,
  Activity,
} from "@lucide/vue";

const router = useRouter();
const taskStore = useTaskStore();

const loadError = ref("");

async function loadTasks() {
  loadError.value = "";
  const result = await taskStore.loadTasks();
  if (!result.ok) {
    loadError.value = result.error?.message ?? "加载任务列表失败";
  }
}

function selectAndOpen(taskId: string) {
  taskStore.selectTask(taskId); // 内部已处理 activeRunId + subscribe
  router.push("/");
}

function statusLabel(status: string): string {
  const s = status as string;
  switch (s) {
    case "pending": return "待处理";
    case "running": return "运行中";
    case "waiting_for_user": return "等待用户";
    case "waiting_for_permission": return "等待授权";
    case "blocked": return "已阻塞";
    case "failed": return "失败";
    case "completed": return "已完成";
    case "cancelled": return "已取消";
    case "queued": return "已排队";
    default: return s;
  }
}

function statusColor(status: string): string {
  const s = status as string;
  switch (s) {
    case "completed": return "text-green-600";
    case "running": case "queued": return "text-blue-600";
    case "failed": return "text-red-600";
    case "cancelled": return "text-amber-600";
    case "waiting_for_permission": return "text-amber-600";
    default: return "text-gray-500";
  }
}

onMounted(() => {
  loadTasks();
});
</script>

<template>
  <div class="flex flex-col h-full overflow-auto">
    <!-- 标题栏 -->
    <div class="flex items-center justify-between px-4 py-3 border-b border-[var(--color-border)]">
      <div class="flex items-center gap-2">
        <ListTodo :size="18" class="text-[var(--color-muted)]" />
        <span class="font-medium text-[var(--color-text)]">任务列表</span>
        <span class="text-xs text-[var(--color-muted)]">({{ taskStore.tasks.length }})</span>
      </div>
    </div>

    <!-- 加载中 -->
    <div v-if="taskStore.loading" class="flex items-center justify-center py-20">
      <Loader2 :size="24" class="animate-spin text-[var(--color-muted)]" />
    </div>

    <!-- 加载错误 -->
    <div v-else-if="loadError" class="flex flex-col items-center justify-center py-20 gap-2">
      <AlertCircle :size="32" class="text-red-400" />
      <span class="text-sm text-red-500">{{ loadError }}</span>
    </div>

    <!-- 空状态 -->
    <div v-else-if="taskStore.tasks.length === 0" class="flex flex-col items-center justify-center py-20 gap-2">
      <Inbox :size="32" class="text-[var(--color-muted)]" />
      <span class="text-sm text-[var(--color-muted)]">暂无任务</span>
      <span class="text-xs text-[var(--color-border)]">创建新任务后将出现在这里</span>
    </div>

    <!-- 任务列表 -->
    <div v-else class="flex-1 overflow-auto">
      <button
        v-for="task in taskStore.tasks"
        :key="task.id"
        class="w-full flex items-center gap-3 px-4 py-3 border-b border-[var(--color-border)] hover:bg-gray-50 transition-colors text-left"
        :class="{ 'bg-blue-50': task.id === taskStore.activeTaskId }"
        @click="selectAndOpen(task.id)"
      >
        <!-- 状态指示 -->
        <div
          class="w-2 h-2 rounded-full shrink-0"
          :class="{
            'bg-green-500': (task.status as string) === 'completed',
            'bg-blue-500': (task.status as string) === 'running' || (task.status as string) === 'queued',
            'bg-red-500': (task.status as string) === 'failed',
            'bg-amber-500': (task.status as string) === 'waiting_for_permission' || (task.status as string) === 'cancelled',
            'bg-gray-300': (task.status as string) === 'pending',
          }"
        />

        <!-- 内容 -->
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-[var(--color-text)] truncate">
              {{ task.title }}
            </span>
            <span class="text-xs shrink-0" :class="statusColor(task.status as string)">
              {{ statusLabel(task.status as string) }}
            </span>
          </div>
          <div class="text-xs text-[var(--color-muted)] truncate mt-0.5">
            {{ task.user_goal }}
          </div>
          <div class="flex items-center gap-2 mt-1 text-xs text-[var(--color-border)]">
            <Clock :size="10" />
            <span>{{ new Date(task.created_at).toLocaleString() }}</span>
            <span v-if="task.active_run_id" class="flex items-center gap-1 text-blue-400">
              <Activity :size="10" />
              run
            </span>
          </div>
        </div>

        <!-- 箭头 -->
        <ArrowRight :size="14" class="text-[var(--color-muted)] shrink-0" />
      </button>
    </div>
  </div>
</template>
