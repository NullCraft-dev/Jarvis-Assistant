// Settings Store — 只消费 Gateway SettingsDTO，并管理“下一次任务”的工作区选择。

import { defineStore } from "pinia";
import { computed, ref } from "vue";
import type { SettingsDTO } from "@jarvis/shared";
import { getSettings } from "@/api/client";

const LS_WORKSPACE_KEY = "jarvis_selected_workspace_path";

export const useSettingsStore = defineStore("settings", () => {
  const settings = ref<SettingsDTO | null>(null);
  const selectedWorkspacePath = ref<string | null>(
    localStorage.getItem(LS_WORKSPACE_KEY)
  );
  const loading = ref(false);
  const error = ref<string | null>(null);
  let inflight: Promise<void> | null = null;

  const allowedWorkspacePaths = computed(
    () => settings.value?.workspace.allowed_workspace_paths ?? []
  );

  async function loadSettings() {
    if (settings.value || inflight) return inflight ?? Promise.resolve();

    loading.value = true;
    inflight = (async () => {
      try {
        const result = await getSettings();
        if (!result.ok) {
          error.value = result.error.message || "加载设置失败";
          return;
        }

        settings.value = result.data;
        error.value = null;
        const allowed = result.data.workspace.allowed_workspace_paths;
        const saved = selectedWorkspacePath.value;
        const fallback =
          result.data.workspace.default_workspace_path ?? allowed[0] ?? null;
        setSelectedWorkspacePath(saved && allowed.includes(saved) ? saved : fallback);
      } catch {
        error.value = "设置服务不可用";
      }
    })();

    try {
      await inflight;
    } finally {
      loading.value = false;
      inflight = null;
    }
  }

  function setSelectedWorkspacePath(path: string | null) {
    if (path !== null && !allowedWorkspacePaths.value.includes(path)) return false;
    selectedWorkspacePath.value = path;
    if (path) localStorage.setItem(LS_WORKSPACE_KEY, path);
    else localStorage.removeItem(LS_WORKSPACE_KEY);
    return true;
  }

  return {
    settings,
    selectedWorkspacePath,
    allowedWorkspacePaths,
    loading,
    error,
    loadSettings,
    setSelectedWorkspacePath,
  };
});
