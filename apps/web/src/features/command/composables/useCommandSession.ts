// Command Session Composable — 管理一次任务会话的完整生命周期
// 职责：创建任务 → 订阅 SSE 事件 → 更新 stores → UI 响应式更新
// 真源：docs/13-interface-contract.md, docs/11-frontend-app-ui-design.md

import { ref } from "vue";
import { useTaskStore } from "@/stores/taskStore";
import { useRunStore } from "@/stores/runStore";
import { normalizeClientError } from "@/api/errors";
import type { AppError } from "@jarvis/shared";

export function useCommandSession() {
  const taskStore = useTaskStore();
  const runStore = useRunStore();

  const isSubmitting = ref(false);
  const error = ref<AppError | null>(null);
  const lastUserGoal = ref("");

  /** 发送用户指令，创建任务并开始订阅事件 */
  async function submit(userGoal: string): Promise<boolean> {
    const normalizedGoal = userGoal.trim();
    if (!normalizedGoal) return false;

    isSubmitting.value = true;
    error.value = null;
    lastUserGoal.value = normalizedGoal;

    try {
      const result = await taskStore.createTask(normalizedGoal);
      if (!result.ok) {
        error.value = result.error;
        return false;
      }

      const runId = result.data.run.id;
      runStore.subscribe(runId);
      return true;
    } catch (cause) {
      error.value = normalizeClientError(cause, "任务创建失败，请稍后重试");
      return false;
    } finally {
      isSubmitting.value = false;
    }
  }

  function retry(): Promise<boolean> {
    return lastUserGoal.value ? submit(lastUserGoal.value) : Promise.resolve(false);
  }

  function clearError() {
    error.value = null;
  }

  return {
    isSubmitting,
    error,
    submit,
    retry,
    clearError,
  };
}
