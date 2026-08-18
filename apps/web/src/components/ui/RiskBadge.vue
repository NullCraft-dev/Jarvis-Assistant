<script setup lang="ts">
// 风险等级标签 L0-L5
// 真源：docs/08-permission-security-design.md
import type { RiskLevel } from "@jarvis/shared";

defineProps<{
  level: RiskLevel;
}>();

function riskStyle(level: string) {
  switch (level) {
    case "L0":
      return "bg-gray-100 text-gray-500";
    case "L1":
      return "bg-green-50 text-green-600";
    case "L2":
      return "bg-blue-50 text-blue-600";
    case "L3":
      return "bg-amber-50 text-amber-700 border border-amber-200";
    case "L4":
      return "bg-red-50 text-red-600 border border-red-200";
    case "L5":
      return "bg-red-100 text-red-700 border border-red-300 font-bold";
    default:
      return "bg-gray-100 text-gray-500";
  }
}

function riskLabel(level: string) {
  switch (level) {
    case "L0": return "L0 只读";
    case "L1": return "L1 低风险";
    case "L2": return "L2 范围内写入";
    case "L3": return "L3 需确认";
    case "L4": return "L4 受限";
    case "L5": return "L5 禁止";
    default: return level;
  }
}
</script>

<template>
  <span
    class="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium"
    :class="riskStyle(level)"
    :title="riskLabel(level)"
  >
    {{ level }}
  </span>
</template>
