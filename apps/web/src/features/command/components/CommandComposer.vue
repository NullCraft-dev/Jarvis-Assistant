<script setup lang="ts">
// 底部输入区：多行文本 + 发送按钮
// 真源：docs/11-frontend-app-ui-design.md § Composer

import { Send, Loader2 } from "@lucide/vue";

const props = defineProps<{
  loading?: boolean;
  disabled?: boolean;
}>();

const emit = defineEmits<{
  submit: [text: string];
}>();

const text = defineModel<string>({ default: "" });

function handleSubmit() {
  if (!text.value.trim() || props.loading) return;
  emit("submit", text.value);
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    handleSubmit();
  }
}
</script>

<template>
  <div
    class="shrink-0 border-t border-[var(--color-border)] bg-[var(--color-surface)] p-2 sm:p-3"
  >
    <div class="mx-auto flex min-w-0 max-w-3xl items-end gap-2">
      <textarea
        v-model="text"
        :disabled="disabled"
        class="min-w-0 flex-1 resize-none rounded-lg border border-[var(--color-border)] px-3 py-2 text-sm focus:border-[var(--color-accent)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)] disabled:cursor-not-allowed disabled:opacity-50"
        rows="2"
        placeholder="输入任务指令，Enter 发送，Shift+Enter 换行..."
        @keydown="handleKeydown"
      ></textarea>
      <button
        class="shrink-0 w-9 h-9 flex items-center justify-center rounded-lg bg-[var(--color-accent)] text-white hover:opacity-90 transition-opacity disabled:opacity-50 disabled:cursor-not-allowed"
        :disabled="disabled || !text.trim() || loading"
        @click="handleSubmit"
      >
        <Loader2 v-if="loading" :size="16" class="animate-spin" />
        <Send v-else :size="16" />
      </button>
    </div>
  </div>
</template>
