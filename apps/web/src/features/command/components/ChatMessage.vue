<script setup lang="ts">
// 单条对话消息：用户消息或 Agent 回复
// 真源：docs/11-frontend-app-ui-design.md § ConversationThread

import { User, Bot } from "@lucide/vue";
import MessageContentRenderer from "./MessageContentRenderer.vue";
import MessageFeedback from "@/features/feedback/components/MessageFeedback.vue";

withDefaults(defineProps<{
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
  isTyping?: boolean;
  messageId?: string;
}>(), {
  isTyping: false,
});
</script>

<template>
  <div class="flex min-w-0 gap-2 py-3 sm:gap-3" :class="role === 'user' ? 'flex-row-reverse' : ''">
    <!-- 头像 -->
    <div
      class="shrink-0 w-7 h-7 rounded-full flex items-center justify-center"
      :class="
        role === 'user'
          ? 'bg-[var(--color-accent)] text-white'
          : 'bg-gray-200 text-[var(--color-muted)]'
      "
    >
      <User v-if="role === 'user'" :size="14" />
      <Bot v-else :size="14" />
    </div>

    <!-- 消息内容 -->
    <div
      class="min-w-0 max-w-[88%] overflow-hidden rounded-lg px-3 py-2 text-sm sm:max-w-[78%] lg:max-w-[70%]"
      :class="
        role === 'user'
          ? 'bg-[var(--color-accent)] text-white'
          : 'bg-[var(--color-surface)] border border-[var(--color-border)]'
      "
    >
      <div class="min-w-0">
        <MessageContentRenderer :role="role" :content="content" />
        <span
          v-if="role === 'assistant' && isTyping"
          class="typewriter-cursor ml-0.5 inline-block h-[1em] w-[2px] translate-y-[2px] bg-current"
          aria-hidden="true"
        />
      </div>
      <div
        v-if="timestamp"
        class="text-xs mt-1 opacity-60"
      >
        {{ new Date(timestamp).toLocaleTimeString() }}
      </div>
      <MessageFeedback
        v-if="role === 'assistant' && messageId && !isTyping"
        :message-id="messageId"
        :content="content"
      />
    </div>
  </div>
</template>

<style scoped>
.typewriter-cursor {
  animation: typewriter-blink 0.8s steps(1, end) infinite;
}

@keyframes typewriter-blink {
  50% { opacity: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .typewriter-cursor { animation: none; }
}
</style>
