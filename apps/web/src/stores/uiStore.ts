// UI Store — 管理 UI 布局状态
// 分层：Frontend State，纯 UI 状态，不涉及业务数据
// 真源：docs/11-frontend-app-ui-design.md

import { defineStore } from "pinia";
import { ref } from "vue";

export type InspectorTab = "context" | "tools" | "permissions" | "logs";

export const useUiStore = defineStore("ui", () => {
  const sidebarCollapsed = ref(false);
  const inspectorVisible = ref(true);
  const compactLayout = ref(false);
  const sidebarDrawerOpen = ref(false);
  const inspectorDrawerOpen = ref(false);
  const inspectorTab = ref<InspectorTab>("context");
  const composerDraft = ref("");

  function toggleSidebar() {
    if (compactLayout.value) {
      sidebarDrawerOpen.value = !sidebarDrawerOpen.value;
      if (sidebarDrawerOpen.value) inspectorDrawerOpen.value = false;
      return;
    }
    sidebarCollapsed.value = !sidebarCollapsed.value;
  }

  function toggleInspector() {
    if (compactLayout.value) {
      inspectorDrawerOpen.value = !inspectorDrawerOpen.value;
      if (inspectorDrawerOpen.value) sidebarDrawerOpen.value = false;
      return;
    }
    inspectorVisible.value = !inspectorVisible.value;
  }

  function setInspectorTab(tab: InspectorTab) {
    inspectorTab.value = tab;
  }

  function openInspector(tab: InspectorTab) {
    inspectorTab.value = tab;
    if (compactLayout.value) {
      inspectorDrawerOpen.value = true;
      sidebarDrawerOpen.value = false;
      return;
    }
    inspectorVisible.value = true;
  }

  function setCompactLayout(compact: boolean) {
    compactLayout.value = compact;
    if (!compact) {
      sidebarDrawerOpen.value = false;
      inspectorDrawerOpen.value = false;
    }
  }

  function closeSidebarDrawer() {
    sidebarDrawerOpen.value = false;
  }

  function closeInspectorDrawer() {
    inspectorDrawerOpen.value = false;
  }

  return {
    sidebarCollapsed,
    inspectorVisible,
    compactLayout,
    sidebarDrawerOpen,
    inspectorDrawerOpen,
    inspectorTab,
    composerDraft,
    toggleSidebar,
    toggleInspector,
    setInspectorTab,
    openInspector,
    setCompactLayout,
    closeSidebarDrawer,
    closeInspectorDrawer,
  };
});
