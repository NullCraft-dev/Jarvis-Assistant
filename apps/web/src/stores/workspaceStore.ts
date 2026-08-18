// Workspace Store — 管理 Workspace 注册、选择与 Task 绑定
// 真源：docs/13-interface-contract.md, docs/11-frontend-app-ui-design.md

import { defineStore } from "pinia";
import { computed, ref } from "vue";
import type { WorkspaceDTO, ID } from "@jarvis/shared";
import * as api from "@/api/client";

const LS_WORKSPACE_ID_KEY = "jarvis_selected_workspace_id";

export const useWorkspaceStore = defineStore("workspace", () => {
  const workspaces = ref<WorkspaceDTO[]>([]);
  const selectedWorkspaceId = ref<ID | null>(
    localStorage.getItem(LS_WORKSPACE_ID_KEY) || null
  );
  const loading = ref(false);
  const error = ref<string | null>(null);

  // 竞态防护：每次 load 递增，响应必须匹配当前 generation
  let loadGeneration = 0;

  const activeWorkspaces = computed(() =>
    workspaces.value.filter((ws) => ws.status === "active")
  );

  const selectedWorkspace = computed(() =>
    workspaces.value.find((ws) => ws.id === selectedWorkspaceId.value) ?? null
  );

  /** 为 Workspace 生成可区分的显示名称（同名时显示路径前缀） */
  function displayName(ws: WorkspaceDTO): string {
    const sameName = workspaces.value.filter(
      (w) => w.id !== ws.id && w.name === ws.name
    );
    if (sameName.length === 0) return ws.name;
    // 同名时显示 canonical_path 的上一级目录帮助区分
    const parts = ws.canonical_path.split("/").filter(Boolean);
    if (parts.length >= 2) {
      return `${ws.name} (${parts[parts.length - 2]}/${parts[parts.length - 1]})`;
    }
    return `${ws.name} (${ws.canonical_path})`;
  }

  async function loadWorkspaces() {
    // 每次 load 都发起新请求（不使用 inflight 复用，避免过期数据）
    const gen = ++loadGeneration;
    loading.value = true;
    try {
      const result = await api.listWorkspaces();
      // 竞态防护：只接受最新 generation 的响应
      if (gen !== loadGeneration) return;

      if (!result.ok) {
        error.value = result.error.message || "加载工作区列表失败";
        return;
      }

      workspaces.value = result.data.workspaces;
      error.value = null;

      // 恢复或自动选择 workspace
      const saved = selectedWorkspaceId.value;
      const actives = result.data.workspaces.filter((ws) => ws.status === "active");

      if (saved && actives.some((ws) => ws.id === saved)) {
        // saved ID 仍然 active，保持不变
      } else {
        // saved ID 失效或不存在 → fallback 到第一个 active
        setSelectedWorkspaceId(actives[0]?.id ?? null);
      }
    } catch {
      if (gen !== loadGeneration) return;
      error.value = "工作区服务不可用";
    } finally {
      if (gen === loadGeneration) {
        loading.value = false;
      }
    }
  }

  function setSelectedWorkspaceId(id: ID | null) {
    if (id !== null && !activeWorkspaces.value.some((ws) => ws.id === id)) {
      return false;
    }
    selectedWorkspaceId.value = id;
    if (id) {
      localStorage.setItem(LS_WORKSPACE_ID_KEY, id);
    } else {
      localStorage.removeItem(LS_WORKSPACE_ID_KEY);
    }
    return true;
  }

  async function pickAndAddWorkspace(): Promise<{ cancelled: boolean; error?: string }> {
    error.value = null;
    try {
      const result = await api.pickWorkspace();
      if (!result.ok) {
        error.value = result.error.message || "选择工作区失败";
        return { cancelled: false, error: error.value };
      }

      if (result.data.cancelled) {
        // 用户取消不是错误
        return { cancelled: true };
      }

      if (result.data.workspace) {
        const newWs = result.data.workspace;
        // 立即合并到本地列表并选中（避免整表刷新延迟）
        const idx = workspaces.value.findIndex((ws) => ws.id === newWs.id);
        if (idx >= 0) {
          workspaces.value[idx] = newWs;
        } else {
          workspaces.value.push(newWs);
        }
        setSelectedWorkspaceId(newWs.id);

        // 后台权威刷新（不覆盖当前选择）
        loadWorkspaces();
      }

      return { cancelled: false };
    } catch {
      error.value = "网络异常，请重试";
      return { cancelled: false, error: error.value };
    }
  }

  async function revokeWorkspace(workspaceId: ID): Promise<boolean> {
    error.value = null;
    try {
      const result = await api.revokeWorkspace(workspaceId);
      if (!result.ok) {
        error.value = result.error.message || "撤销工作区失败";
        return false;
      }

      // 如果撤销的是当前选中的，重新选择
      if (selectedWorkspaceId.value === workspaceId) {
        const next = activeWorkspaces.value.find((ws) => ws.id !== workspaceId);
        setSelectedWorkspaceId(next?.id ?? null);
      }

      await loadWorkspaces();
      return true;
    } catch {
      error.value = "网络异常，请重试";
      return false;
    }
  }

  return {
    workspaces,
    activeWorkspaces,
    selectedWorkspaceId,
    selectedWorkspace,
    loading,
    error,
    displayName,
    loadWorkspaces,
    setSelectedWorkspaceId,
    pickAndAddWorkspace,
    revokeWorkspace,
  };
});
