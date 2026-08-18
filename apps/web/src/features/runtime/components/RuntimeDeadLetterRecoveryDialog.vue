<script setup lang="ts">
import { ref, watch } from "vue";
import type {
  DlqRetryInspectionDTO,
  DlqRetryResolutionOutput,
  PermissionRequestDTO,
  RuntimeDeadLetterDTO,
} from "@jarvis/shared";
import { AlertTriangle, CheckCircle2, LoaderCircle, ShieldAlert, X } from "@lucide/vue";

const props = defineProps<{
  open: boolean;
  record: RuntimeDeadLetterDTO | null;
  inspection: DlqRetryInspectionDTO | null;
  request: PermissionRequestDTO | null;
  resolution: DlqRetryResolutionOutput | null;
  loading: boolean;
  error: string | null;
}>();
const emit = defineEmits<{
  close: [];
  createRequest: [];
  resolve: [decision: "allow_once" | "deny", note: string];
}>();
const note = ref("");
watch(() => props.record?.id, () => { note.value = ""; });
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4" @click.self="emit('close')">
      <section role="dialog" aria-modal="true" aria-labelledby="dlq-recovery-title" class="w-full max-w-lg rounded-lg border border-[var(--color-border)] bg-white shadow-xl">
        <header class="flex items-start justify-between border-b border-[var(--color-border)] px-5 py-4">
          <div><div class="flex items-center gap-2"><ShieldAlert :size="18" class="text-amber-600" /><h2 id="dlq-recovery-title" class="font-medium text-[var(--color-text)]">DLQ 受控重试</h2></div><p class="mt-1 text-xs text-[var(--color-muted)]">L3 · 必须单次确认 · 原 DLQ 记录会保留</p></div>
          <button aria-label="关闭" class="rounded p-1 text-[var(--color-muted)] hover:bg-gray-100" :disabled="loading" @click="emit('close')"><X :size="17" /></button>
        </header>

        <div class="space-y-4 px-5 py-4 text-sm">
          <div v-if="record" class="rounded border border-[var(--color-border)] bg-gray-50 p-3 text-xs">
            <div class="font-mono font-medium text-red-600">{{ record.error_code }}</div>
            <div class="mt-2 grid gap-1 text-[var(--color-muted)]"><span>Task: {{ record.task_id || "—" }}</span><span>Run: {{ record.run_id || "—" }}</span><span>DLQ: {{ record.id }}</span></div>
          </div>
          <div v-if="loading && !inspection" class="flex items-center gap-2 py-5 text-[var(--color-muted)]"><LoaderCircle :size="17" class="animate-spin" />正在核对 PostgreSQL 权威状态…</div>
          <div v-else-if="inspection" :class="inspection.eligible ? 'border-emerald-200 bg-emerald-50' : 'border-amber-200 bg-amber-50'" class="rounded border p-3">
            <div class="flex items-center gap-2 font-medium" :class="inspection.eligible ? 'text-emerald-700' : 'text-amber-700'"><CheckCircle2 v-if="inspection.eligible" :size="17" /><AlertTriangle v-else :size="17" />{{ inspection.eligible ? "符合受控重试条件" : "当前不可处置" }}</div>
            <p class="mt-1 text-xs text-[var(--color-muted)]">{{ inspection.reason }}</p>
            <p v-if="inspection.eligible" class="mt-2 text-xs text-[var(--color-muted)]">批准后会从 PostgreSQL 重新读取任务目标和工作区，创建全新的 Run；不会重放 Redis payload，也不会复用旧 Run。</p>
          </div>
          <div v-if="request?.status === 'pending'" class="space-y-2">
            <label class="block text-xs font-medium text-[var(--color-text)]" for="dlq-recovery-note">决定备注（可选）</label>
            <textarea id="dlq-recovery-note" v-model="note" maxlength="500" rows="3" class="w-full resize-none rounded border border-[var(--color-border)] p-2 text-xs outline-none focus:border-blue-500" placeholder="说明本次人工处置原因" />
          </div>
          <div v-if="resolution?.new_run" class="rounded border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-700"><div class="flex items-center gap-2 font-medium"><CheckCircle2 :size="17" />新 Run 已进入队列</div><div class="mt-1 break-all font-mono text-xs">{{ resolution.new_run.id }}</div></div>
          <div v-else-if="resolution?.request.status === 'denied'" class="rounded border border-gray-200 bg-gray-50 p-3 text-xs text-[var(--color-muted)]">本次重试已拒绝并写入审计日志。</div>
          <div v-if="error" class="rounded border border-red-200 bg-red-50 p-3 text-xs text-red-600">{{ error }}</div>
        </div>

        <footer class="flex flex-wrap justify-end gap-2 border-t border-[var(--color-border)] px-5 py-3">
          <button class="rounded border border-[var(--color-border)] px-3 py-2 text-xs" :disabled="loading" @click="emit('close')">关闭</button>
          <button v-if="inspection?.eligible && !request" class="rounded bg-[var(--color-accent)] px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="loading" @click="emit('createRequest')">创建单次确认请求</button>
          <template v-if="request?.status === 'pending'">
            <button class="rounded border border-red-200 px-3 py-2 text-xs text-red-600 disabled:opacity-50" :disabled="loading" @click="emit('resolve', 'deny', note)">拒绝并审计</button>
            <button class="rounded bg-amber-600 px-3 py-2 text-xs text-white disabled:opacity-50" :disabled="loading" @click="emit('resolve', 'allow_once', note)"><LoaderCircle v-if="loading" :size="14" class="mr-1 inline animate-spin" />确认创建新 Run</button>
          </template>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
