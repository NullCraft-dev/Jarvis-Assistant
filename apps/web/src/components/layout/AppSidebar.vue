<script setup lang="ts">
// 左侧导航栏
// 多轮对话 MVP：显示会话列表，支持会话切换和恢复
// 真源：docs/11-frontend-app-ui-design.md § Sidebar

import { useUiStore } from "@/stores/uiStore";
import { useRouter, useRoute } from "vue-router";
import { ref, onMounted, onUnmounted, computed } from "vue";
import { getWorkers } from "@/api/client";
import { useTaskStore } from "@/stores/taskStore";
import { useSettingsStore } from "@/stores/settingsStore";
import type { WorkerStatusDTO, ModelStatusDTO, ConversationDTO } from "@jarvis/shared";
import {
  MessageSquarePlus,
  ListTodo,
  Bot,
  Brain,
  Wrench,
  Settings,
  ScrollText,
  Database,
  MessageSquare,
  Activity,
  BookOpen,
  CalendarClock,
} from "@lucide/vue";

const props = withDefaults(defineProps<{ drawer?: boolean }>(), {
  drawer: false,
});

const ui = useUiStore();
const taskStore = useTaskStore();
const settingsStore = useSettingsStore();
const router = useRouter();
const route = useRoute();

const storageBackend = ref<string | null>(null);
const storageLabel = ref("...");
const workers = ref<WorkerStatusDTO[]>([]);
let _pollTimer: ReturnType<typeof setInterval> | null = null;

onMounted(async () => {
  const loadSettings = async () => {
    await settingsStore.loadSettings();
    storageBackend.value = settingsStore.settings?.runtime.storage_backend ?? null;
    const v = storageBackend.value;
    if (v === "inmemory") storageLabel.value = "In-Memory";
    else if (v === "postgresql") storageLabel.value = "PostgreSQL";
    else if (v === null) storageLabel.value = "?";
    else storageLabel.value = "?";
  };

  // Worker 状态轮询
  const _poll = async () => {
    try {
      const r = await getWorkers();
      if (r.ok) workers.value = r.data.workers;
    } catch { /* keep last known */ }
  };

  // 独立容错初始化：任一请求失败都不阻止其他状态加载。
  await Promise.allSettled([
    loadSettings(),
    // 浏览器缓存中的会话 ID 先经过服务端验证，再恢复为当前选择。
    taskStore.restoreConversation(),
    taskStore.loadConversations(),
    taskStore.loadTasks(),
    _poll(),
  ]);
  _pollTimer = setInterval(_poll, 5000);
});

onUnmounted(() => {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
});

const modelStatus = computed<{ label: string; dotClass: string }>(() => {
  const online = workers.value.filter((w: WorkerStatusDTO) => !w.is_stale);
  if (online.length === 0) return { label: "Worker 未连接", dotClass: "bg-gray-400" };
  const m: ModelStatusDTO | undefined =
    online.find((worker) => worker.model?.status === "configured")?.model
    ?? online.find((worker) => worker.model)?.model;
  if (!m) return { label: "Model 未配置", dotClass: "bg-amber-400" };
  if (m.status === "configured") return { label: m.model_name || "已配置", dotClass: "bg-green-500" };
  return { label: "Model 未配置", dotClass: "bg-amber-400" };
});

interface NavItem {
  label: string;
  icon: any;
  activeOn?: string;    // route path 匹配时高亮
  route?: string;       // 点击跳转
  action?: "new-task";
  disabled?: boolean;
}

const navItems: NavItem[] = [
  { label: "New Task", icon: MessageSquarePlus, route: "/", action: "new-task" },
  { label: "Tasks", icon: ListTodo, route: "/tasks", activeOn: "/tasks" },
  { label: "Agents", icon: Bot, disabled: true },
  { label: "Memory", icon: Brain, route: "/memory", activeOn: "/memory" },
  { label: "Knowledge", icon: BookOpen, route: "/knowledge", activeOn: "/knowledge" },
  { label: "Schedules", icon: CalendarClock, route: "/schedules", activeOn: "/schedules" },
  { label: "Tools", icon: Wrench, route: "/tools", activeOn: "/tools" },
  { label: "Audit", icon: ScrollText, route: "/audit-logs", activeOn: "/audit-logs" },
  { label: "Runtime", icon: Activity, route: "/runtime-health", activeOn: "/runtime-health" },
  { label: "Settings", icon: Settings, route: "/settings", activeOn: "/settings" },
];

function handleClick(item: NavItem) {
  if (item.disabled) return;
  if (item.action === "new-task") {
    taskStore.startNewTask();
    ui.setInspectorTab("context");
  }
  if (item.route) {
    router.push(item.route);
  }
  if (props.drawer) ui.closeSidebarDrawer();
}

function handleConvClick(conv: ConversationDTO) {
  taskStore.selectConversation(conv.id);
  if (route.path !== "/") {
    router.push("/");
  }
  if (props.drawer) ui.closeSidebarDrawer();
}

function isActive(item: NavItem): boolean {
  return !!item.activeOn && (
    route.path === item.activeOn || route.path.startsWith(`${item.activeOn}/`)
  );
}

/** 当前是否选中该会话 */
function isConvActive(convId: string): boolean {
  return taskStore.activeConversationId === convId;
}
</script>

<template>
  <aside
    class="flex h-full shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-surface)] transition-all duration-200"
    :class="[
      drawer ? 'w-[min(19rem,calc(100vw-3rem))]' : 'w-[200px]',
      !drawer && ui.sidebarCollapsed ? '!w-0 overflow-hidden' : '',
    ]"
    :aria-label="drawer ? '导航抽屉' : '主导航'"
  >
    <!-- 导航项 -->
    <nav class="flex-1 py-2 overflow-y-auto">
      <button
        v-for="item in navItems"
        :key="item.label"
        :disabled="item.disabled"
        :title="item.disabled ? `${item.label} 暂未开放` : undefined"
        class="w-full flex items-center gap-2 px-4 py-2 text-sm transition-colors text-left"
        :class="
          item.disabled
            ? 'text-gray-300 cursor-not-allowed'
            : isActive(item)
            ? 'bg-blue-50 text-blue-700 font-medium border-r-2 border-blue-500'
            : 'text-[var(--color-muted)] hover:bg-gray-50 hover:text-[var(--color-text)]'
        "
        @click="handleClick(item)"
      >
        <component :is="item.icon" :size="16" />
        <span>{{ item.label }}</span>
      </button>

      <!-- 多轮对话 MVP：最近会话列表 -->
      <div
        v-if="taskStore.conversations.length > 0 || taskStore.conversationListError"
        class="mt-3 border-t border-[var(--color-border)] pt-2"
      >
        <p class="px-4 py-1 text-[11px] text-[var(--color-muted)] uppercase tracking-wide">
          最近会话
        </p>
        <!-- 会话列表加载错误 -->
        <div v-if="taskStore.conversationListError" class="px-4 py-1">
          <span class="text-[11px] text-red-500">{{ taskStore.conversationListError }}</span>
          <button
            class="text-[11px] ml-1 text-blue-600 hover:text-blue-800"
            @click="taskStore.loadConversations()"
          >重试</button>
        </div>
        <button
          v-for="conv in taskStore.conversations.slice(0, 10)"
          :key="conv.id"
          class="w-full flex items-center gap-2 px-4 py-1.5 text-xs transition-colors text-left"
          :class="
            isConvActive(conv.id)
              ? 'bg-blue-50 text-blue-700 font-medium border-r-2 border-blue-500'
              : 'text-[var(--color-muted)] hover:bg-gray-50 hover:text-[var(--color-text)]'
          "
          @click="handleConvClick(conv)"
        >
          <MessageSquare :size="12" class="shrink-0" />
          <span class="min-w-0 flex-1 truncate">
            {{ conv.title || '未命名会话' }}
          </span>
        </button>
      </div>
    </nav>

    <!-- 底部系统状态 -->
    <div class="p-3 border-t border-[var(--color-border)] text-xs text-[var(--color-muted)] space-y-1">
      <div class="flex items-center gap-1.5">
        <span class="w-2 h-2 rounded-full bg-green-500"></span>
        Runtime 已连接
      </div>
      <div class="flex items-center gap-1.5">
        <Database :size="12" />
        Storage {{ storageLabel }}
      </div>
      <div class="flex items-center gap-1.5">
        <span class="w-2 h-2 rounded-full" :class="modelStatus.dotClass"></span>
        {{ modelStatus.label }}
      </div>
    </div>
  </aside>
</template>
