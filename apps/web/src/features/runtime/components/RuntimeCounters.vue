<script setup lang="ts">
import type { RuntimeHealthCountersDTO } from "@jarvis/shared";
defineProps<{ counters: RuntimeHealthCountersDTO }>();
const rows = [
  { name: "Run Queue", prefix: "run" },
  { name: "Worker Command", prefix: "command" },
  { name: "Runtime Event", prefix: "event" },
] as const;
</script>
<template>
  <section class="overflow-hidden rounded-lg border border-[var(--color-border)] bg-white"><div class="border-b border-[var(--color-border)] px-4 py-3"><h2 class="text-sm font-medium text-[var(--color-text)]">可靠性累计指标</h2><p class="mt-1 text-xs text-[var(--color-muted)]">来自当前 Worker 与 Gateway 进程；进程重启后重新累计。</p></div><div class="grid divide-y divide-[var(--color-border)] md:grid-cols-3 md:divide-x md:divide-y-0"><div v-for="row in rows" :key="row.prefix" class="p-4"><div class="text-xs font-medium text-[var(--color-text)]">{{ row.name }}</div><dl class="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs"><div><dt class="text-[var(--color-muted)]">Reclaimed</dt><dd class="mt-0.5 font-medium tabular-nums">{{ counters[`${row.prefix}_reclaimed`] }}</dd></div><div v-if="row.prefix !== 'command'"><dt class="text-[var(--color-muted)]">Retry deferred</dt><dd class="mt-0.5 font-medium tabular-nums">{{ counters[`${row.prefix}_retry_deferred`] }}</dd></div><div><dt class="text-[var(--color-muted)]">Malformed</dt><dd class="mt-0.5 font-medium tabular-nums">{{ counters[`${row.prefix}_malformed`] }}</dd></div><div><dt class="text-[var(--color-muted)]">Dead-lettered</dt><dd class="mt-0.5 font-medium tabular-nums">{{ counters[`${row.prefix}_dead_lettered`] }}</dd></div></dl></div></div></section>
</template>
