<script setup lang="ts">
import type { RuntimeHealthDTO } from "@jarvis/shared";
import { Activity, Bot, CircleAlert, Inbox } from "@lucide/vue";
defineProps<{ health: RuntimeHealthDTO }>();
const statusLabels = { healthy: "运行正常", degraded: "需要关注", unavailable: "诊断不可用" };
const statusClasses = { healthy: "border-emerald-200 bg-emerald-50 text-emerald-700", degraded: "border-amber-200 bg-amber-50 text-amber-700", unavailable: "border-gray-200 bg-gray-50 text-gray-600" };
</script>
<template>
  <div class="grid gap-3 md:grid-cols-3">
    <section class="rounded-lg border border-[var(--color-border)] bg-white p-4"><div class="flex items-center justify-between"><span class="text-xs text-[var(--color-muted)]">整体状态</span><Activity :size="16" /></div><div class="mt-3"><span class="rounded-full border px-2.5 py-1 text-sm font-medium" :class="statusClasses[health.status]">{{ statusLabels[health.status] }}</span></div><p class="mt-3 text-xs text-[var(--color-muted)]">{{ health.runtime_bus === "redis" ? "Redis Runtime Bus" : "In-memory Runtime" }}</p></section>
    <section class="rounded-lg border border-[var(--color-border)] bg-white p-4"><div class="flex items-center justify-between"><span class="text-xs text-[var(--color-muted)]">Worker</span><Bot :size="16" /></div><div class="mt-2 text-2xl font-semibold text-[var(--color-text)]">{{ health.workers.online }}<span class="text-sm font-normal text-[var(--color-muted)]"> / {{ health.workers.total }} 在线</span></div><p class="mt-2 text-xs text-[var(--color-muted)]">{{ health.workers.busy }} busy · {{ health.workers.stale }} stale</p></section>
    <section class="rounded-lg border border-[var(--color-border)] bg-white p-4"><div class="flex items-center justify-between"><span class="text-xs text-[var(--color-muted)]">Dead Letter</span><Inbox :size="16" /></div><div class="mt-2 text-2xl font-semibold text-[var(--color-text)]">{{ health.dead_letters.reduce((sum, item) => sum + item.count, 0) }}</div><p class="mt-2 flex items-center gap-1 text-xs text-[var(--color-muted)]"><CircleAlert :size="12" />仅诊断副本，不是业务真源</p></section>
  </div>
</template>
