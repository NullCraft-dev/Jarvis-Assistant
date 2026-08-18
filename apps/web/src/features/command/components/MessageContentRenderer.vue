<script setup lang="ts">
import { computed } from "vue";
import { renderAssistantContent } from "../messageContent";

const props = defineProps<{
  role: "user" | "assistant";
  content: string;
}>();

const rendered = computed(() =>
  props.role === "assistant" ? renderAssistantContent(props.content) : null,
);
</script>

<template>
  <div v-if="role === 'user'" class="max-w-full whitespace-pre-wrap break-words [overflow-wrap:anywhere]">{{ content }}</div>
  <div
    v-else-if="rendered?.kind === 'markdown'"
    class="message-markdown min-w-0 max-w-full break-words"
    v-html="rendered.html"
  />
  <div v-else-if="rendered?.kind === 'json'" class="message-json min-w-0 max-w-full overflow-hidden rounded-md border border-slate-200 bg-slate-950">
    <div class="border-b border-slate-700 px-3 py-1 text-[10px] font-medium uppercase tracking-wide text-slate-400">JSON</div>
    <pre class="max-h-80 max-w-full overflow-auto p-3 text-xs leading-5 text-slate-100"><code>{{ rendered.formatted }}</code></pre>
  </div>
</template>

<style scoped>
.message-markdown :deep(p) {
  margin: 0.45rem 0;
  overflow-wrap: anywhere;
}

.message-markdown :deep(p:first-child),
.message-markdown :deep(h1:first-child),
.message-markdown :deep(h2:first-child),
.message-markdown :deep(h3:first-child) {
  margin-top: 0;
}

.message-markdown :deep(p:last-child),
.message-markdown :deep(ul:last-child),
.message-markdown :deep(ol:last-child),
.message-markdown :deep(pre:last-child) {
  margin-bottom: 0;
}

.message-markdown :deep(h1),
.message-markdown :deep(h2),
.message-markdown :deep(h3) {
  margin: 0.8rem 0 0.35rem;
  font-weight: 650;
  line-height: 1.3;
}

.message-markdown :deep(h1) { font-size: 1.15rem; }
.message-markdown :deep(h2) { font-size: 1.05rem; }
.message-markdown :deep(h3) { font-size: 0.95rem; }

.message-markdown :deep(ul),
.message-markdown :deep(ol) {
  margin: 0.45rem 0;
  padding-left: 1.35rem;
}

.message-markdown :deep(ul) { list-style: disc; }
.message-markdown :deep(ol) { list-style: decimal; }
.message-markdown :deep(li + li) { margin-top: 0.2rem; }

.message-markdown :deep(blockquote) {
  margin: 0.55rem 0;
  border-left: 3px solid #cbd5e1;
  padding-left: 0.75rem;
  color: #475569;
  overflow-wrap: anywhere;
}

.message-markdown :deep(code) {
  border-radius: 0.25rem;
  background: #eef2f7;
  padding: 0.08rem 0.3rem;
  font-size: 0.82rem;
  overflow-wrap: anywhere;
}

.message-markdown :deep(pre) {
  margin: 0.55rem 0;
  max-height: 20rem;
  overflow: auto;
  border-radius: 0.4rem;
  background: #0f172a;
  padding: 0.75rem;
  color: #f8fafc;
}

.message-markdown :deep(pre code) {
  background: transparent;
  padding: 0;
  color: inherit;
  white-space: pre;
  overflow-wrap: normal;
}

.message-markdown :deep(a) {
  color: #2563eb;
  text-decoration: underline;
  text-underline-offset: 2px;
  overflow-wrap: anywhere;
}

.message-markdown :deep(img) {
  max-width: 100%;
  height: auto;
}

.message-markdown :deep(.katex-display) {
  max-width: 100%;
  overflow-x: auto;
  overflow-y: hidden;
  padding: 0.2rem 0;
}

.message-markdown :deep(.katex-display > .katex) {
  text-align: center;
}

.message-markdown :deep(table) {
  display: block;
  max-width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
  margin: 0.55rem 0;
}

.message-markdown :deep(th),
.message-markdown :deep(td) {
  min-width: 9rem;
  max-width: 18rem;
  border: 1px solid #dbe2ea;
  padding: 0.35rem 0.55rem;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}

.message-markdown :deep(th) {
  background: #f8fafc;
  font-weight: 600;
}
</style>
