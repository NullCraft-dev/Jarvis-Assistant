<script setup lang="ts">
// 空状态占位
import { Inbox } from "@lucide/vue";

defineProps<{
  title?: string;
  description?: string;
}>();
</script>

<template>
  <div class="flex flex-col items-center justify-center py-16 text-center">
    <Inbox :size="40" class="text-[var(--color-border)] mb-3" />
    <p class="text-sm font-medium text-[var(--color-muted)]">
      {{ title ?? "暂无数据" }}
    </p>
    <p v-if="description" class="text-xs text-[var(--color-border)] mt-1">
      {{ description }}
    </p>
  </div>
</template>
