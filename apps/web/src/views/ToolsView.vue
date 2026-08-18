<script setup lang="ts">
import { onMounted, ref } from "vue";
import { Plug, RefreshCw } from "@lucide/vue";
import { useMcpStore } from "@/stores/mcpStore";

const store = useMcpStore();
const name = ref("");
const slug = ref("");
const command = ref("");
const args = ref("");
const envKeys = ref("");

onMounted(store.load);

async function submit() {
  const ok = await store.create({
    name: name.value.trim(),
    slug: slug.value.trim(),
    command: command.value.trim(),
    args: args.value.split("\n").map((value) => value.trim()).filter(Boolean),
    env_keys: envKeys.value.split(",").map((value) => value.trim()).filter(Boolean),
  });
  if (ok) {
    name.value = "";
    slug.value = "";
    command.value = "";
    args.value = "";
    envKeys.value = "";
  }
}
</script>

<template>
  <div class="h-full overflow-auto">
    <header class="border-b border-[var(--color-border)] px-5 py-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <Plug :size="18" />
          <h1 class="font-medium">MCP 工具</h1>
        </div>
        <button class="flex items-center gap-1 rounded border bg-white px-2.5 py-1.5 text-xs" :disabled="store.saving" @click="store.refresh">
          <RefreshCw :size="13" />重新发现
        </button>
      </div>
      <p class="mt-1 text-xs text-[var(--color-muted)]">连接本机已有的 MCP server。发现到的工具默认需要每次确认后才能执行。</p>
    </header>

    <main class="mx-auto max-w-2xl space-y-5 p-5">
      <p v-if="store.error" class="rounded border border-red-200 bg-red-50 p-3 text-xs text-red-600">{{ store.error }}</p>
      <p v-if="store.refreshCommandId" class="rounded border border-blue-200 bg-blue-50 p-3 text-xs text-blue-800">
        发现请求已交给 Worker。命令编号：{{ store.refreshCommandId }}
      </p>
      <p v-if="store.restartRequired" class="rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
        发现结果会保存到数据库；请稍后刷新页面确认状态，并重启 Worker 让 Agent 使用新工具。
      </p>

      <section class="rounded-lg border border-blue-100 bg-blue-50/60 p-4">
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 class="text-sm font-medium text-blue-950">权威文献来源</h2>
            <p class="mt-1 text-xs leading-5 text-blue-800">
              连接 Jarvis 内置的 arXiv 元数据 MCP。它只检索题目、作者、摘要和规范链接，不直接写本地文件。
            </p>
          </div>
          <button
            class="shrink-0 rounded bg-blue-600 px-3 py-2 text-xs text-white disabled:opacity-50"
            :disabled="store.saving || store.servers.some((item) => item.slug === 'jarvis_literature')"
            @click="store.connectLiterature"
          >
            {{ store.servers.some((item) => item.slug === "jarvis_literature") ? "已连接" : "一键连接" }}
          </button>
        </div>
      </section>

      <section class="space-y-3 rounded-lg border bg-white p-4">
        <h2 class="text-sm font-medium">连接 stdio server</h2>
        <div class="grid gap-3">
          <input v-model="name" maxlength="200" class="rounded border px-3 py-2 text-sm" placeholder="显示名称" />
          <input v-model="slug" maxlength="64" class="rounded border px-3 py-2 text-sm" placeholder="唯一标识，如 literature" />
        </div>
        <input v-model="command" maxlength="2048" class="w-full rounded border px-3 py-2 text-sm" placeholder="可执行文件绝对路径" />
        <textarea v-model="args" class="min-h-20 w-full rounded border px-3 py-2 text-sm" placeholder="启动参数，每行一个" />
        <input v-model="envKeys" class="w-full rounded border px-3 py-2 text-sm" placeholder="允许传递的环境变量名，用逗号分隔（不填写值）" />
        <p class="text-xs text-[var(--color-muted)]">Jarvis 只保存环境变量名称，不保存密钥值。注册 server 属于高风险配置操作并写入审计。</p>
        <button class="rounded bg-blue-600 px-4 py-2 text-xs text-white disabled:opacity-50" :disabled="store.saving || !name.trim() || !slug.trim() || !command.trim()" @click="submit">
          保存连接
        </button>
      </section>

      <section>
        <h2 class="mb-2 text-sm font-medium">已连接 {{ store.servers.length }}</h2>
        <div v-if="!store.servers.length" class="rounded border border-dashed p-6 text-center text-xs text-[var(--color-muted)]">尚未配置 MCP server</div>
        <div v-else class="space-y-2">
          <article v-for="server in store.servers" :key="server.id" class="rounded border bg-white p-4">
            <div class="flex justify-between gap-3">
              <div>
                <p class="text-sm font-medium">{{ server.name }}</p>
                <p class="mt-1 break-all text-xs text-[var(--color-muted)]">{{ server.command }} {{ server.args.join(" ") }}</p>
              </div>
              <span class="h-fit rounded px-2 py-1 text-xs" :class="server.status === 'connected' ? 'bg-green-50 text-green-700' : server.status === 'error' ? 'bg-red-50 text-red-700' : 'bg-gray-100 text-gray-600'">
                {{ server.status }}
              </span>
            </div>
            <p v-if="server.last_error_code" class="mt-2 text-xs text-red-600">{{ server.last_error_code }}</p>
            <div class="mt-3 flex flex-wrap gap-1">
              <span v-for="tool in server.tools" :key="tool.id" class="rounded bg-gray-100 px-2 py-1 text-xs">{{ tool.internal_name }} · {{ tool.risk_level }}</span>
              <span v-if="!server.tools.length" class="text-xs text-[var(--color-muted)]">尚未发现工具</span>
            </div>
            <button class="mt-3 rounded border px-2.5 py-1.5 text-xs" :disabled="store.saving" @click="store.toggle(server)">
              {{ server.enabled ? "停用" : "启用" }}
            </button>
          </article>
        </div>
      </section>
    </main>
  </div>
</template>
