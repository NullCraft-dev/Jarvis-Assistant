<script setup lang="ts">
import type { AppError } from "@jarvis/shared";
import type { EventConnectionState } from "@/api/transport";
import type { RunStatusPresentation } from "@/features/command/composables/runPresentation";
import { AlertTriangle, CheckCircle2, LoaderCircle, RotateCcw, WifiOff } from "@lucide/vue";

defineProps<{
  presentation: RunStatusPresentation;
  connectionState: EventConnectionState;
  runError?: AppError | null;
  operationError?: AppError | null;
  canRetryStep?: boolean;
  canReconnect?: boolean;
  retrying?: boolean;
}>();

defineEmits<{
  retryStep: [];
  reconnect: [];
  dismissError: [];
}>();

const toneClasses = {
  neutral: "border-gray-200 bg-gray-50 text-gray-700",
  info: "border-sky-200 bg-sky-50 text-sky-800",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  success: "border-emerald-200 bg-emerald-50 text-emerald-800",
  danger: "border-red-200 bg-red-50 text-red-800",
};
</script>

<template>
  <section
    class="border-t px-2 py-2.5 sm:px-4"
    :class="toneClasses[presentation.tone]"
    aria-live="polite"
  >
    <div class="mx-auto flex min-w-0 max-w-3xl flex-col gap-2.5 sm:flex-row sm:items-start">
      <CheckCircle2 v-if="presentation.tone === 'success'" :size="16" class="mt-0.5 shrink-0" />
      <AlertTriangle v-else-if="presentation.tone === 'danger' || presentation.tone === 'warning'" :size="16" class="mt-0.5 shrink-0" />
      <LoaderCircle v-else :size="16" class="mt-0.5 shrink-0" :class="{ 'animate-spin': presentation.tone === 'info' }" />

      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
          <strong class="text-xs">{{ presentation.label }}</strong>
          <span
            v-if="connectionState === 'connecting' || connectionState === 'reconnecting'"
            class="inline-flex items-center gap-1 rounded-full border border-current/20 px-1.5 py-0.5 text-[11px]"
          >
            <WifiOff :size="11" />
            {{ connectionState === "connecting" ? "连接事件流" : "事件流重连中" }}
          </span>
        </div>
        <p class="mt-0.5 break-words text-xs opacity-80 [overflow-wrap:anywhere]">{{ presentation.description }}</p>

        <div
          v-if="runError || operationError"
          class="mt-2 rounded border border-current/15 bg-white/60 px-2.5 py-2 text-xs"
        >
          <p class="break-words font-medium [overflow-wrap:anywhere]">{{ (operationError || runError)?.message }}</p>
          <p class="mt-1 break-words opacity-70 [overflow-wrap:anywhere]">
            错误码 {{ (operationError || runError)?.code }}
            · {{ (operationError || runError)?.recoverable ? "可以重试" : "需要调整任务或检查配置" }}
          </p>
        </div>
      </div>

      <div class="flex w-full shrink-0 flex-wrap justify-end gap-1.5 sm:w-auto">
        <button
          v-if="canReconnect && (connectionState === 'reconnecting' || connectionState === 'closed')"
          class="rounded border border-current/30 bg-white/70 px-2 py-1 text-xs hover:bg-white"
          @click="$emit('reconnect')"
        >
          重新连接
        </button>
        <button
          v-if="canRetryStep"
          class="inline-flex items-center gap-1 rounded border border-current/30 bg-white/70 px-2 py-1 text-xs hover:bg-white disabled:opacity-50"
          :disabled="retrying"
          @click="$emit('retryStep')"
        >
          <RotateCcw :size="12" />
          {{ retrying ? "创建恢复运行..." : "从安全检查点恢复" }}
        </button>
        <button
          v-if="operationError"
          class="rounded px-2 py-1 text-xs opacity-70 hover:bg-white/70"
          @click="$emit('dismissError')"
        >
          关闭
        </button>
      </div>
    </div>
  </section>
</template>
