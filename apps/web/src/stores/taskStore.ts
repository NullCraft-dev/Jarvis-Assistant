// Task Store — 管理任务列表、当前活跃任务和会话
// 分层：Frontend State，只消费 DTO，不私造业务状态
// 真源：docs/13-interface-contract.md, docs/11-frontend-app-ui-design.md

import { defineStore } from "pinia";
import { ref, computed, watch } from "vue";
import type { TaskDTO, ConversationDTO, ID } from "@jarvis/shared";
import * as api from "@/api/client";
import { useRunStore } from "@/stores/runStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";
import { usePermissionStore } from "@/stores/permissionStore";

const LS_CONVERSATION_KEY = "jarvis_active_conversation_id";

export const useTaskStore = defineStore("task", () => {
  const tasks = ref<TaskDTO[]>([]);
  const conversations = ref<ConversationDTO[]>([]);
  const activeTaskId = ref<ID | null>(null);
  const activeRunId = ref<ID | null>(null);
  /** 仅用于当前页面的回复动画；历史恢复/会话选择不得设置。 */
  const localPresentationRunId = ref<ID | null>(null);
  // 浏览器缓存不是业务真源。启动时先保持未选择状态，只有
  // restoreConversation() 向服务端验证成功后才恢复缓存中的会话。
  const activeConversationId = ref<ID | null>(null);
  const loading = ref(false);

  /** 会话历史刷新计数——用于 ConversationThread 检测竞态 */
  const historyVersion = ref(0);
  /** 会话列表加载错误（侧栏展示/重试用） */
  const conversationListError = ref<string | null>(null);

  // 持久化 activeConversationId 到 localStorage（页面刷新可恢复）
  watch(activeConversationId, (val) => {
    if (val) {
      localStorage.setItem(LS_CONVERSATION_KEY, val);
    } else {
      localStorage.removeItem(LS_CONVERSATION_KEY);
    }
    // 注意：切换 conversation 时 ConversationThread 的 watch(activeConversationId)
    // 会触发一次 refresh()。historyVersion 只表达"同一 conversation 内消息变化"，
    // 不在此递增——避免一次切换触发两次 refresh。
  });

  const activeTask = computed(() =>
    tasks.value.find((t) => t.id === activeTaskId.value) ?? null
  );

  /**
   * 恢复会话最近一次 Task/Run，使 SSE 能从 PostgreSQL 重放运行历史。
   * TaskDTO 是后端状态真源；这里只负责选择和订阅，不推导运行结论。
   */
  function activateLatestTaskForConversation(convId: ID) {
    const latestTask = tasks.value
      .filter((task) => task.conversation_id === convId)
      .reduce<TaskDTO | null>((latest, task) => {
        if (!latest) return task;
        return task.updated_at > latest.updated_at ? task : latest;
      }, null);

    const previousRunId = activeRunId.value;
    const nextRunId = latestTask?.active_run_id ?? null;
    localPresentationRunId.value = null;
    activeTaskId.value = latestTask?.id ?? null;
    activeRunId.value = nextRunId;

    const runStore = useRunStore();
    if (previousRunId && previousRunId !== nextRunId) {
      runStore.unsubscribe(previousRunId);
    }
    if (
      nextRunId &&
      (previousRunId !== nextRunId || runStore.getEvents(nextRunId).length === 0)
    ) {
      runStore.resubscribe(nextRunId);
      usePermissionStore().loadPendingForRun(nextRunId);
    }
  }

  async function createTask(userGoal: string) {
    loading.value = true;
    try {
      const oldConvId = activeConversationId.value;

      let result = await api.createTask({
        user_goal: userGoal,
        conversation_id: oldConvId ?? undefined,
        workspace_id: useWorkspaceStore().selectedWorkspaceId ?? undefined,
      });
      // 会话可能在页面打开后被另一环境清理。仅针对明确的 not_found
      // 自动清除失效选择并重试一次，避免用户首次发送被浏览器缓存阻断。
      if (
        !result.ok &&
        oldConvId &&
        (result.error.code === "NOT_FOUND" || result.error.category === "not_found")
      ) {
        startNewTask();
        result = await api.createTask({
          user_goal: userGoal,
          workspace_id: useWorkspaceStore().selectedWorkspaceId ?? undefined,
        });
      }
      if (!result.ok) {
        loading.value = false;
        return result;
      }

      tasks.value.unshift(result.data.task);
      activeTaskId.value = result.data.task.id;
      activeRunId.value = result.data.run.id;
      localPresentationRunId.value = result.data.run.id;

      const newConvId = result.data.conversation.id;
      const convChanged = newConvId !== oldConvId;
      activeConversationId.value = newConvId;

      // 刷新侧栏"最近会话"（失败不影响已成功的 Task）
      loadConversations().catch(() => {
        conversationListError.value = "会话列表刷新失败";
      });

      // 只触发一次刷新：
      // - 会话变化 → activeConversationId watcher 触发
      // - 会话未变 → 手动递增 historyVersion
      if (!convChanged) {
        historyVersion.value++;
      }
      // 注意：convChanged=true 时 historyVersion 不递增，
      // 由 watch(activeConversationId) 负责触发 refresh

      return result;
    } finally {
      loading.value = false;
    }
  }

  async function loadTasks() {
    loading.value = true;
    try {
      const result = await api.listTasks();
      if (result.ok) {
        tasks.value = result.data.tasks;
        if (activeConversationId.value) {
          activateLatestTaskForConversation(activeConversationId.value);
        }
      }
      return result;
    } finally {
      loading.value = false;
    }
  }

  /** 加载会话列表 */
  async function loadConversations() {
    try {
      const result = await api.listConversations();
      if (result.ok) {
        conversations.value = result.data.conversations;
        conversationListError.value = null;
      } else {
        conversationListError.value = result.error.message || "加载会话列表失败";
      }
      return result;
    } catch {
      conversationListError.value = "网络异常，请重试";
      return { ok: false as const, error: { code: "NETWORK", message: "网络异常", category: "internal" as const, recoverable: true } };
    }
  }

  function selectTask(taskId: ID) {
    localPresentationRunId.value = null;
    activeTaskId.value = taskId;
    const task = tasks.value.find((t) => t.id === taskId);
    activeRunId.value = task?.active_run_id ?? null;
    activeConversationId.value = task?.conversation_id ?? null;
    if (task?.active_run_id) {
      const runStore = useRunStore();
      runStore.resubscribe(task.active_run_id);
      usePermissionStore().loadPendingForRun(task.active_run_id);
    }
  }

  function activateReplacementRun(runId: ID) {
    const task = tasks.value.find((item) => item.id === activeTaskId.value);
    if (task) {
      task.active_run_id = runId;
      task.status = "running";
      task.updated_at = new Date().toISOString();
    }
    activeRunId.value = runId;
    localPresentationRunId.value = runId;
    useRunStore().resubscribe(runId);
    historyVersion.value++;
  }

  /** 选择一个会话并加载其历史 */
  function selectConversation(convId: ID) {
    activeConversationId.value = convId;
    activateLatestTaskForConversation(convId);
    localStorage.setItem(LS_CONVERSATION_KEY, convId);
  }

  /** 从 localStorage 恢复上一次的会话选中状态。缓存 ID 必须先由服务端验证。 */
  async function restoreConversation() {
    const saved = localStorage.getItem(LS_CONVERSATION_KEY);
    if (!saved) return;

    const result = await api.getConversation(saved, { limit: 1 });
    if (!result.ok) {
      if (result.error.code === "NOT_FOUND" || result.error.category === "not_found") {
        localStorage.removeItem(LS_CONVERSATION_KEY);
      }
      return result;
    }

    activeConversationId.value = saved;
    if (tasks.value.length > 0) {
      activateLatestTaskForConversation(saved);
    }
    return result;
  }

  /** 开始一条全新的任务会话，不复用当前 conversation */
  function startNewTask() {
    if (activeRunId.value) {
      useRunStore().unsubscribe(activeRunId.value);
    }
    activeTaskId.value = null;
    activeRunId.value = null;
    localPresentationRunId.value = null;
    activeConversationId.value = null;
    localStorage.removeItem(LS_CONVERSATION_KEY);
  }

  return {
    tasks,
    conversations,
    activeTaskId,
    activeRunId,
    localPresentationRunId,
    activeConversationId,
    loading,
    historyVersion,
    conversationListError,
    activeTask,
    createTask,
    loadTasks,
    loadConversations,
    selectTask,
    activateReplacementRun,
    selectConversation,
    restoreConversation,
    startNewTask,
  };
});
