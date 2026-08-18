import { createRouter, createWebHistory } from "vue-router";
import CommandView from "@/views/CommandView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      name: "command",
      component: CommandView,
    },
    {
      path: "/tasks",
      name: "tasks",
      component: () => import("@/views/TaskListView.vue"),
    },
    {
      path: "/memory",
      name: "memory",
      component: () => import("@/views/MemoryView.vue"),
    },
    {
      path: "/knowledge",
      name: "knowledge",
      component: () => import("@/views/KnowledgeView.vue"),
      children: [
        {
          path: "",
          name: "knowledge-overview",
          component: () => import("@/views/KnowledgeOverviewView.vue"),
        },
        {
          path: "documents",
          name: "knowledge-documents",
          component: () => import("@/views/KnowledgeDocumentsView.vue"),
        },
        {
          path: "rag",
          name: "knowledge-rag",
          component: () => import("@/views/KnowledgeRagView.vue"),
        },
        {
          path: "quality",
          name: "knowledge-quality",
          component: () => import("@/views/KnowledgeQualityView.vue"),
        },
      ],
    },
    {
      path: "/schedules",
      name: "schedules",
      component: () => import("@/views/ScheduleView.vue"),
    },
    {
      path: "/tools",
      name: "tools",
      component: () => import("@/views/ToolsView.vue"),
    },
    {
      path: "/settings",
      name: "settings",
      component: () => import("@/views/SettingsView.vue"),
    },
    {
      path: "/audit-logs",
      name: "audit-logs",
      component: () => import("@/views/AuditLogView.vue"),
    },
    {
      path: "/runtime-health",
      name: "runtime-health",
      component: () => import("@/views/RuntimeHealthView.vue"),
    },
  ],
});

export default router;
