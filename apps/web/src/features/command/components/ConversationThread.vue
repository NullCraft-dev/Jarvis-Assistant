<script setup lang="ts">
// 对话线程：多轮消息 + Agent 回复 + InlineRunBlock
// 多轮对话 MVP：持久化 API 为真源，实时 Run 内容仅为临时展示
//
// 竞态保护：useConversationHistory 内置递增计数器，每次 refresh() 分配唯一 token，
// 只有最新请求才能更新 historyMessages。会话切换、同一会话快速追问、Run 终态刷新均正确工作。
//
// 真源：docs/11-frontend-app-ui-design.md § Command Chat

import { computed, nextTick, ref, watch } from "vue";
import type { ID } from "@jarvis/shared";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useConversationHistory } from "@/features/command/composables/useConversationHistory";
import { shouldProjectLiveRunText } from "@/features/command/conversationProjection";
import { useTypewriterText } from "@/features/command/composables/useTypewriterText";
import ChatMessage from "./ChatMessage.vue";
import InlineRunBlock from "@/features/timeline/components/InlineRunBlock.vue";

const taskStore = useTaskStore();
const runStore = useRunStore();

const { historyMessages, isLoading, isLoadingOlder, historyError, loadOlderError, nextCursor, refresh, loadOlder } = useConversationHistory();

const threadScroller = ref<HTMLElement | null>(null);
const shouldFollowOutput = ref(true);
const liveRunId = computed(() => taskStore.activeRunId ?? null);
const shouldAnimateLiveOutput = computed(
  () =>
    Boolean(taskStore.activeRunId) &&
    taskStore.localPresentationRunId === taskStore.activeRunId
);
const liveTargetContent = computed(() => {
  const runId = taskStore.activeRunId;
  if (!runId || !shouldAnimateLiveOutput.value) return "";
  // terminal output 是持久化真源；到达后也继续由打字机缓冲平滑展示。
  return runStore.getFinalOutputText(runId) || runStore.getStreamingText(runId);
});
const { displayedText: liveDisplayedText, isTyping: isTypingLiveOutput } =
  useTypewriterText(liveTargetContent, liveRunId, shouldAnimateLiveOutput);

function handleThreadScroll() {
  const element = threadScroller.value;
  if (!element) return;
  shouldFollowOutput.value = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
}

watch(liveRunId, () => {
  shouldFollowOutput.value = true;
});

watch(liveDisplayedText, async () => {
  if (!shouldFollowOutput.value) return;
  await nextTick();
  const element = threadScroller.value;
  element?.scrollTo({ top: element.scrollHeight, behavior: "auto" });
});

/** 会话切换 → 全量刷新 */
watch(
  () => taskStore.activeConversationId,
  (convId) => { refresh(convId); },
  { immediate: true }
);

/** 新任务创建或当前 Run 进入终态 → 刷新持久化历史 */
watch(
  () => taskStore.historyVersion,
  () => { refresh(taskStore.activeConversationId); }
);

/** 监听当前 active run 的终态事件，触发刷新 */
watch(
  () => {
    const rid = taskStore.activeRunId;
    return rid ? runStore.getStatus(rid) : null;
  },
  (newStatus, oldStatus) => {
    if (
      newStatus &&
      oldStatus &&
      oldStatus !== newStatus &&
      (newStatus === "completed" || newStatus === "failed" || newStatus === "cancelled")
    ) {
      refresh(taskStore.activeConversationId);
    }
  }
);

interface DisplayMessage {
  key: string;       // 稳定唯一 key（消息 ID 或临时 ID）
  role: "user" | "assistant";
  content: string;
  timestamp: string;
  runId?: ID;
  isTyping?: boolean;
  messageId?: ID;
}

const messages = computed<DisplayMessage[]>(() => {
  const msgs: DisplayMessage[] = [];

  // 1. 持久化消息（唯一真源）
  for (const m of historyMessages.value) {
    if (m.role === "user" || m.role === "assistant") {
      const isActiveRunReply =
        m.role === "assistant" &&
        shouldProjectLiveRunText(
          runStore.getStatus(m.run_id ?? ""),
          Boolean(liveTargetContent.value),
        ) &&
        m.run_id === taskStore.activeRunId;
      msgs.push({
        key: m.id,
        role: m.role as "user" | "assistant",
        content: isActiveRunReply ? liveDisplayedText.value : m.content,
        timestamp: m.created_at,
        runId: m.run_id ?? undefined,
        isTyping: isActiveRunReply && isTypingLiveOutput.value,
        messageId: m.id,
      });
    }
  }

  // 2. 当前活跃 Run 的实时 assistant 回复（尚未持久化时的临时展示）
  //    使用 task_id 判断是否属于当前活跃任务
  if (taskStore.activeRunId) {
    const liveContent = liveTargetContent.value;

    if (liveContent && taskStore.activeTaskId) {
      // 检查此回复是否已作为持久化消息存在（以 task_id + run_id 匹配）
      const activeTaskId = taskStore.activeTaskId;
      const alreadyPersisted = historyMessages.value.some(
        (m) =>
          m.role === "assistant" &&
          m.task_id === activeTaskId &&
          m.run_id === taskStore.activeRunId
      );
      if (!alreadyPersisted && liveDisplayedText.value) {
        msgs.push({
          key: `live-${taskStore.activeRunId}`,
          role: "assistant",
          content: liveDisplayedText.value,
          timestamp: new Date().toISOString(),
          runId: taskStore.activeRunId,
          isTyping: isTypingLiveOutput.value,
        });
      }
    }
  }

  return msgs;
});
</script>

<template>
  <div
    ref="threadScroller"
    class="min-w-0 flex-1 overflow-y-auto px-2 py-2 sm:px-4"
    @scroll="handleThreadScroll"
  >
    <!-- 空状态 -->
    <div
      v-if="!taskStore.activeTask && historyMessages.length === 0"
      class="flex items-center justify-center h-full"
    >
      <div class="text-center">
        <p class="text-lg font-medium text-[var(--color-text)] mb-1">
          Jarvis Assistant
        </p>
        <p class="text-sm text-[var(--color-muted)]">
          在下方输入指令，让 Agent 帮你完成任务
        </p>
      </div>
    </div>

    <!-- 对话流 -->
    <div v-else class="mx-auto min-w-0 max-w-3xl">
      <!-- 会话刷新错误 -->
      <div
        v-if="historyError"
        class="text-center py-2"
      >
        <span class="text-xs text-red-500">{{ historyError.message }}</span>
        <button
          class="text-xs ml-2 text-blue-600 hover:text-blue-800"
          @click="refresh(taskStore.activeConversationId)"
        >重试</button>
      </div>

      <!-- 加载更早消息 -->
      <div
        v-if="nextCursor && !historyError"
        class="text-center py-2"
      >
        <button
          class="text-xs text-blue-600 hover:text-blue-800 transition-colors disabled:text-gray-400"
          :disabled="isLoading || isLoadingOlder"
          @click="loadOlder"
        >
          {{ isLoading ? "会话刷新中..." : isLoadingOlder ? "加载中..." : "加载更早消息" }}
        </button>
      </div>

      <!-- 分页加载错误 -->
      <div
        v-if="loadOlderError"
        class="text-center py-1"
      >
        <span class="text-xs text-red-500">{{ loadOlderError.message }}</span>
        <button
          class="text-xs ml-2 text-blue-600 hover:text-blue-800"
          @click="loadOlder"
        >重试</button>
      </div>

      <ChatMessage
        v-for="msg in messages"
        :key="msg.key"
        :role="msg.role"
        :content="msg.content"
        :timestamp="msg.timestamp"
        :is-typing="msg.isTyping"
        :message-id="msg.messageId"
      />

      <!-- 内嵌运行块 -->
      <InlineRunBlock
        v-if="taskStore.activeRunId"
        :run-id="taskStore.activeRunId"
      />
    </div>
  </div>
</template>
