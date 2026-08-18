<script setup lang="ts">
import { Settings } from "@lucide/vue";
import WorkspaceManager from "@/features/workspaces/components/WorkspaceManager.vue";
import ModelConfigPanel from "@/features/settings/components/ModelConfigPanel.vue";
</script>

<template>
  <div class="h-full overflow-auto">
    <div class="border-b border-[var(--color-border)] px-5 py-4">
      <div class="flex items-center gap-2">
        <Settings :size="18" class="text-[var(--color-muted)]" />
        <h1 class="font-medium text-[var(--color-text)]">设置</h1>
      </div>
      <p class="mt-1 text-xs text-[var(--color-muted)]">管理模型配置、工作区绑定与本地 Agent 运行参数。</p>
    </div>
    <div class="mx-auto max-w-4xl p-5 space-y-8">
      <ModelConfigPanel />
      <WorkspaceManager />
    </div>
  </div>
</template>
