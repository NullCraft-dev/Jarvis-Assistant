<script setup lang="ts">
// 任务/运行状态标签
// 颜色遵循 docs/11-frontend-app-ui-design.md § Status Badge

defineProps<{
  status: string;
}>();

function statusStyle(status: string) {
  switch (status) {
    case "pending":
    case "created":
      return "bg-gray-100 text-gray-600";
    case "running":
      return "bg-blue-50 text-blue-600";
    case "waiting_for_user":
    case "waiting_for_permission":
      return "bg-amber-50 text-amber-600";
    case "blocked":
      return "bg-orange-50 text-orange-600";
    case "failed":
      return "bg-red-50 text-red-600";
    case "completed":
      return "bg-green-50 text-green-600";
    case "cancelled":
    case "skipped":
      return "bg-gray-100 text-gray-400";
    default:
      return "bg-gray-100 text-gray-600";
  }
}
</script>

<template>
  <span
    class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium"
    :class="statusStyle(status)"
  >
    {{ status }}
  </span>
</template>
