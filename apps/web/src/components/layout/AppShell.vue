<script setup lang="ts">
// 主布局：三栏结构（Sidebar | Main | Inspector）
// 真源：docs/11-frontend-app-ui-design.md

import AppHeader from "./AppHeader.vue";
import AppSidebar from "./AppSidebar.vue";
import RightInspector from "@/features/inspector/components/RightInspector.vue";
import { useUiStore } from "@/stores/uiStore";
import { onMounted, onUnmounted } from "vue";

const ui = useUiStore();
let compactQuery: MediaQueryList | null = null;

function syncCompactLayout() {
  ui.setCompactLayout(compactQuery?.matches ?? false);
}

onMounted(() => {
  compactQuery = window.matchMedia("(max-width: 1023px)");
  syncCompactLayout();
  compactQuery.addEventListener("change", syncCompactLayout);
});

onUnmounted(() => {
  compactQuery?.removeEventListener("change", syncCompactLayout);
  compactQuery = null;
});
</script>

<template>
  <div class="h-screen w-screen flex flex-col bg-[var(--color-bg)]">
    <!-- Header -->
    <AppHeader />

    <!-- Body -->
    <div class="flex min-w-0 flex-1 overflow-hidden">
      <!-- 宽屏 Sidebar；窄窗口使用下面的 overlay drawer。 -->
      <div class="hidden h-full lg:flex">
        <AppSidebar />
      </div>

      <!-- Main Content -->
      <main class="flex min-w-0 flex-1 flex-col overflow-hidden">
        <slot />
      </main>

      <!-- Right Inspector -->
      <transition name="slide">
        <div v-if="ui.inspectorVisible" class="hidden lg:block">
          <RightInspector />
        </div>
      </transition>

      <!-- 窄窗口 Sidebar drawer，不占主内容宽度。 -->
      <transition name="fade">
        <div
          v-if="ui.compactLayout && ui.sidebarDrawerOpen"
          class="fixed inset-x-0 bottom-0 top-11 z-40 lg:hidden"
        >
          <button
            class="absolute inset-0 bg-slate-950/25"
            aria-label="关闭导航抽屉"
            @click="ui.closeSidebarDrawer()"
          />
          <div class="absolute inset-y-0 left-0 max-w-[calc(100vw-3rem)] shadow-xl">
            <AppSidebar drawer />
          </div>
        </div>
      </transition>

      <!-- 窄窗口 Inspector drawer，同样覆盖显示而不挤压 Command Center。 -->
      <transition name="fade">
        <div
          v-if="ui.compactLayout && ui.inspectorDrawerOpen"
          class="fixed inset-x-0 bottom-0 top-11 z-50 lg:hidden"
        >
          <button
            class="absolute inset-0 bg-slate-950/25"
            aria-label="关闭检查器抽屉"
            @click="ui.closeInspectorDrawer()"
          />
          <div class="absolute inset-y-0 right-0 max-w-[calc(100vw-2rem)] shadow-xl">
            <RightInspector drawer />
          </div>
        </div>
      </transition>
    </div>
  </div>
</template>

<style scoped>
.slide-enter-active,
.slide-leave-active {
  transition: width 0.2s ease;
  overflow: hidden;
}
.slide-enter-from,
.slide-leave-to {
  width: 0;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
