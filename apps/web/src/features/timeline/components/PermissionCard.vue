<script setup lang="ts">
// 权限确认卡片：只消费 PermissionRequestDTO，展示影响、范围、脱敏参数和决定反馈
// 真源：docs/08-permission-security-design.md, docs/11-frontend-app-ui-design.md

import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import type { PermissionDecisionType, PermissionRequestDTO } from "@jarvis/shared";
import { usePermissionStore } from "@/stores/permissionStore";
import RiskBadge from "@/components/ui/RiskBadge.vue";
import {
  formatPermissionArguments,
  getPermissionDecisionPresentation,
  getPermissionScopePresentation,
  getRiskImpact,
} from "@/features/permission/permissionPresentation";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Clock3,
  Info,
  LoaderCircle,
  Shield,
  X,
} from "@lucide/vue";

const props = defineProps<{
  request: PermissionRequestDTO;
}>();

const permStore = usePermissionStore();

const nowMs = ref(Date.now());
let expiryTimer: ReturnType<typeof setInterval> | undefined;
const expiryMs = computed(() => Date.parse(props.request.expires_at));
const hasValidExpiry = computed(() => Number.isFinite(expiryMs.value));
const isPersistedPending = computed(
  () => !props.request.status || props.request.status === "pending",
);
const isExpired = computed(
  () =>
    props.request.status === "expired" ||
    (isPersistedPending.value &&
      (!hasValidExpiry.value || nowMs.value >= expiryMs.value)),
);
const isPending = computed(() => isPersistedPending.value && !isExpired.value);
const scopePresentation = computed(() => getPermissionScopePresentation(props.request.scope));
const argumentRows = computed(() => formatPermissionArguments(props.request.arguments_summary));
const resolvingDecision = computed(() => permStore.getResolvingDecision(props.request.id));
const error = computed(() => permStore.getError(props.request.id));
const resolvedDecision = computed(() =>
  props.request.decision
    ? getPermissionDecisionPresentation(props.request.decision)
    : null
);
const visibleDecisions = computed(() =>
  props.request.allowed_decisions.filter((decision) => {
    if (props.request.risk_level === "L5") return decision === "deny";
    if (
      props.request.risk_level === "L4" &&
      getPermissionDecisionPresentation(decision).persistent
    ) {
      return false;
    }
    return true;
  })
);

const expiryLabel = computed(() => {
  if (!hasValidExpiry.value) return "截止时间无效，已停止授权";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(expiryMs.value));
});

onMounted(() => {
  if (!isPersistedPending.value) return;
  expiryTimer = setInterval(() => {
    nowMs.value = Date.now();
    if (!isPersistedPending.value || isExpired.value) {
      clearInterval(expiryTimer);
      expiryTimer = undefined;
    }
  }, 1_000);
});

onBeforeUnmount(() => {
  if (expiryTimer !== undefined) clearInterval(expiryTimer);
});

function decisionClasses(decision: PermissionDecisionType) {
  const tone = getPermissionDecisionPresentation(decision).tone;
  if (tone === "deny") {
    return "border-red-200 bg-red-50 text-red-700 hover:bg-red-100";
  }
  if (tone === "caution") {
    return "border-amber-300 bg-white text-amber-800 hover:bg-amber-100";
  }
  return "border-emerald-300 bg-emerald-50 text-emerald-800 hover:bg-emerald-100";
}

function submitDecision(decision: PermissionDecisionType) {
  permStore.resolveRequest({ request_id: props.request.id, decision });
}
</script>

<template>
  <section
    class="my-3 overflow-hidden rounded-lg border"
    :class="isPending ? 'border-amber-300 bg-amber-50' : isExpired ? 'border-slate-300 bg-slate-50' : 'border-emerald-200 bg-emerald-50'"
    :aria-label="isPending ? '待处理权限请求' : isExpired ? '已过期权限请求' : '权限决定反馈'"
  >
    <header class="flex items-start justify-between gap-3 border-b border-current/10 px-3 py-2.5">
      <div class="flex min-w-0 items-start gap-2">
        <Shield v-if="isPending" :size="17" class="mt-0.5 shrink-0 text-amber-700" />
        <Clock3 v-else-if="isExpired" :size="17" class="mt-0.5 shrink-0 text-slate-600" />
        <X
          v-else-if="request.status === 'denied' || request.decision === 'deny'"
          :size="17"
          class="mt-0.5 shrink-0 text-red-600"
        />
        <CheckCircle2 v-else :size="17" class="mt-0.5 shrink-0 text-emerald-700" />
        <div class="min-w-0">
          <h3 class="text-sm font-semibold" :class="isPending ? 'text-amber-900' : isExpired ? 'text-slate-800' : 'text-emerald-900'">
            {{
              isPending
                ? "需要你的确认"
                : isExpired
                  ? "授权请求已过期"
                  : request.decision === "deny"
                  ? "操作已拒绝"
                  : "授权决定已受理"
            }}
          </h3>
          <p class="mt-0.5 break-words text-xs text-[var(--color-muted)]">
            {{ request.action_summary }}
          </p>
        </div>
      </div>
      <RiskBadge :level="request.risk_level" />
    </header>

    <div class="space-y-3 px-3 py-3 text-sm">
      <div class="grid gap-2 sm:grid-cols-2">
        <div class="rounded border border-current/10 bg-white/70 p-2.5">
          <div class="text-[11px] font-medium uppercase tracking-wide text-[var(--color-muted)]">执行能力</div>
          <div class="mt-1 break-all font-mono text-xs font-medium text-[var(--color-text)]">
            {{ request.tool_name }}
          </div>
        </div>
        <div class="rounded border border-current/10 bg-white/70 p-2.5">
          <div class="text-[11px] font-medium uppercase tracking-wide text-[var(--color-muted)]">影响范围</div>
          <div class="mt-1 text-xs font-medium text-[var(--color-text)]">
            {{ scopePresentation.label }}
          </div>
          <p class="mt-1 text-xs text-[var(--color-muted)]">
            {{ scopePresentation.description }}
          </p>
        </div>
      </div>

      <dl
        v-if="scopePresentation.facts.length"
        class="grid gap-x-3 gap-y-1 rounded border border-current/10 bg-white/70 p-2.5 text-xs sm:grid-cols-[auto_1fr]"
      >
        <template v-for="fact in scopePresentation.facts" :key="fact.label">
          <dt class="text-[var(--color-muted)]">{{ fact.label }}</dt>
          <dd class="min-w-0 break-all font-mono text-[var(--color-text)]">{{ fact.value }}</dd>
        </template>
      </dl>

      <div v-if="request.reason" class="rounded border border-current/10 bg-white/70 p-2.5 text-xs">
        <div class="font-medium text-[var(--color-text)]">为什么需要确认</div>
        <p class="mt-1 break-words text-[var(--color-muted)]">{{ request.reason }}</p>
      </div>

      <div class="rounded border border-current/10 bg-white/70 p-2.5">
        <div class="text-xs font-medium text-[var(--color-text)]">参数安全摘要</div>
        <dl v-if="argumentRows.length" class="mt-2 space-y-1.5 text-xs">
          <div
            v-for="row in argumentRows"
            :key="row.key"
            class="grid gap-0.5 sm:grid-cols-[minmax(7rem,auto)_1fr] sm:gap-3"
          >
            <dt class="text-[var(--color-muted)]">{{ row.label }}</dt>
            <dd
              class="min-w-0 break-all text-[var(--color-text)]"
              :class="{ 'font-mono': row.monospace }"
            >
              {{ row.value }}
            </dd>
          </div>
        </dl>
        <p v-else class="mt-1 text-xs text-[var(--color-muted)]">没有可展示的参数。</p>
      </div>

      <div class="flex items-start gap-1.5 text-xs text-amber-800">
        <AlertTriangle v-if="request.risk_level === 'L4' || request.risk_level === 'L5'" :size="13" class="mt-0.5 shrink-0" />
        <Info v-else :size="13" class="mt-0.5 shrink-0" />
        <span>{{ getRiskImpact(request.risk_level) }}</span>
      </div>

      <div class="flex items-start gap-1.5 text-xs" :class="isExpired ? 'text-slate-700' : 'text-[var(--color-muted)]'">
        <Clock3 :size="13" class="mt-0.5 shrink-0" />
        <span>{{ isExpired ? "该请求已失效，操作不会执行。" : `授权有效期至 ${expiryLabel}` }}</span>
      </div>

      <div v-if="isPending" class="grid gap-2 sm:grid-cols-2">
        <button
          v-for="decision in visibleDecisions"
          :key="decision"
          class="flex w-full items-start gap-2 rounded border px-3 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50"
          :class="decisionClasses(decision)"
          :disabled="permStore.isResolving(request.id)"
          @click="submitDecision(decision)"
        >
          <LoaderCircle
            v-if="resolvingDecision === decision"
            :size="15"
            class="mt-0.5 shrink-0 animate-spin"
          />
          <Check
            v-else-if="getPermissionDecisionPresentation(decision).tone !== 'deny'"
            :size="15"
            class="mt-0.5 shrink-0"
          />
          <X v-else :size="15" class="mt-0.5 shrink-0" />
          <span class="min-w-0">
            <span class="block text-xs font-medium">
              {{
                resolvingDecision === decision
                  ? "正在提交决定…"
                  : getPermissionDecisionPresentation(decision).label
              }}
            </span>
            <span class="mt-0.5 block text-[11px] opacity-80">
              {{ getPermissionDecisionPresentation(decision).description }}
            </span>
          </span>
        </button>
        <p v-if="visibleDecisions.length === 0" class="rounded bg-red-50 p-2 text-xs text-red-700 sm:col-span-2">
          当前请求没有可用的安全决定，请取消运行或检查权限配置。
        </p>
      </div>

      <div
        v-else-if="!isExpired"
        class="flex items-start gap-2 rounded border border-current/10 bg-white/70 p-2.5 text-xs"
      >
        <Clock3 v-if="request.decision !== 'deny'" :size="14" class="mt-0.5 shrink-0" />
        <CheckCircle2 v-else :size="14" class="mt-0.5 shrink-0" />
        <div>
          <p class="font-medium">
            {{ resolvedDecision?.label ?? "决定已记录" }}
          </p>
          <p class="mt-1 text-[var(--color-muted)]">
            {{
              request.decision === "deny"
                ? "操作不会执行；拒绝结果仍会进入审计和后续运行事件。"
                : "这里只表示授权决定已被服务端接受；工具是否成功仍以后续工具和运行事件为准。"
            }}
          </p>
        </div>
      </div>

      <div
        v-else
        class="flex items-start gap-2 rounded border border-slate-200 bg-white/70 p-2.5 text-xs text-slate-700"
      >
        <Clock3 :size="14" class="mt-0.5 shrink-0" />
        <p>授权窗口已关闭；服务端会以过期终态记录并审计，本次操作不会执行。</p>
      </div>

      <div
        v-if="error"
        class="flex items-start justify-between gap-2 rounded border border-red-200 bg-red-50 p-2.5 text-xs text-red-700"
        role="alert"
      >
        <div>
          <p class="font-medium">{{ error.message }}</p>
          <p class="mt-1 opacity-75">
            错误码 {{ error.code }} · {{ error.recoverable ? "可以重新提交决定" : "请求状态需要重新核对" }}
          </p>
        </div>
        <button class="shrink-0 rounded px-1 py-0.5 hover:bg-red-100" @click="permStore.clearError(request.id)">
          关闭
        </button>
      </div>
    </div>
  </section>
</template>
