<script setup lang="ts">
import { BookOpen, Database, FileText, LayoutDashboard, ShieldCheck } from "@lucide/vue";
import { useRoute } from "vue-router";

const sections = [
  { label: "总览", route: "/knowledge", icon: LayoutDashboard, exact: true },
  { label: "知识文档", route: "/knowledge/documents", icon: FileText },
  { label: "RAG 文档", route: "/knowledge/rag", icon: Database },
  { label: "RAG 质量", route: "/knowledge/quality", icon: ShieldCheck },
];
const route = useRoute();

function isSectionActive(section: (typeof sections)[number]) {
  return section.exact
    ? route.path === section.route
    : route.path === section.route || route.path.startsWith(`${section.route}/`);
}
</script>

<template>
  <div class="flex h-full min-w-0 flex-col overflow-hidden">
    <header class="shrink-0 border-b border-[var(--color-border)] bg-white px-4 pt-4 sm:px-5">
      <div class="flex items-start gap-3 pb-4">
        <div class="mt-0.5 rounded-lg bg-blue-50 p-2 text-blue-600">
          <BookOpen :size="18" />
        </div>
        <div class="min-w-0">
          <h1 class="font-medium">知识中心</h1>
          <p class="mt-1 text-xs leading-5 text-[var(--color-muted)]">
            分别管理人可阅读的知识文档、模型可检索的 RAG 资产与检索质量。
          </p>
        </div>
      </div>

      <nav class="-mb-px flex min-w-0 gap-1 overflow-x-auto" aria-label="知识中心导航">
        <RouterLink
          v-for="section in sections"
          :key="section.route"
          :to="section.route"
          class="flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2.5 text-xs transition-colors"
          :class="isSectionActive(section)
            ? 'border-blue-500 font-medium text-blue-700'
            : 'border-transparent text-[var(--color-muted)] hover:border-gray-300 hover:text-[var(--color-text)]'"
        >
          <component :is="section.icon" :size="14" />
          {{ section.label }}
        </RouterLink>
      </nav>
    </header>

    <RouterView />
  </div>
</template>
