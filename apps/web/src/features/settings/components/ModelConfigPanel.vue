<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import {
  NButton,
  NTag,
  NSpace,
  NDescriptions,
  NDescriptionsItem,
  NAlert,
  NSpin,
  useMessage,
} from "naive-ui";
import {
  Zap,
  Plug,
  Globe,
  Key,
  Clock,
  RefreshCw,
  Brain,
  Activity,
  AlertTriangle,
  CheckCircle,
  XCircle,
} from "@lucide/vue";
import { getModelConfig, testModelConnection, getWorkers } from "@/api/client";
import type { ModelConfigDTO, ModelTestOutput, WorkerStatusDTO } from "@jarvis/shared";

const message = useMessage();

// -- 状态 --
const loading = ref(true);
const config = ref<ModelConfigDTO | null>(null);
const workers = ref<WorkerStatusDTO[]>([]);
const testResult = ref<ModelTestOutput | null>(null);
const testing = ref(false);
const loadError = ref<string | null>(null);

// -- 计算属性 --
const apiKeyStatus = computed(() =>
  config.value?.api_key_configured ? "已配置" : "未配置"
);

const apiKeyType = computed(() =>
  config.value?.api_key_configured ? "success" : "warning"
);

const testButtonLabel = computed(() => {
  if (testing.value) return "测试中...";
  return "测试连接";
});

const testButtonDisabled = computed(
  () => testing.value || !config.value?.provider
);

const providerLabel = computed(() => {
  if (config.value?.provider === "deepseek") return "DeepSeek";
  if (config.value?.provider === "custom_openai_compatible") {
    return "自定义 OpenAI-compatible";
  }
  return config.value?.provider || "未配置";
});

// -- 生命周期 --
async function loadData() {
  loading.value = true;
  loadError.value = null;

  const [configResult, workersResult] = await Promise.allSettled([
    getModelConfig(),
    getWorkers(),
  ]);

  if (configResult.status === "fulfilled" && configResult.value.ok) {
    config.value = configResult.value.data;
  } else {
    loadError.value = "无法加载模型配置";
  }

  if (workersResult.status === "fulfilled" && workersResult.value.ok) {
    workers.value = workersResult.value.data.workers;
  }
  // workers 失败不阻塞（模型配置仍然可展示）

  loading.value = false;
}

async function handleTest() {
  if (testing.value) return;
  testing.value = true;
  testResult.value = null;

  try {
    const result = await testModelConnection();
    if (result.ok) {
      testResult.value = result.data;
      if (result.data.status === "ok") {
        message.success(`连接成功，延迟 ${result.data.latency_ms.toFixed(0)}ms`);
      } else {
        message.warning(
          result.data.error?.message || "连接测试失败"
        );
      }
    } else {
      message.error(result.error?.message || "测试请求失败");
    }
  } catch {
    message.error("网络错误，无法完成测试");
  } finally {
    testing.value = false;
  }
}

onMounted(loadData);
</script>

<template>
  <div class="model-config-panel">
    <div class="panel-header">
      <div class="flex items-center gap-2">
        <Brain :size="18" class="text-[var(--color-muted)]" />
        <h2 class="font-medium text-[var(--color-text)]">模型配置</h2>
      </div>
      <p class="mt-1 text-xs text-[var(--color-muted)]">
        当前运行中模型的配置状态与连通性验证。
      </p>
    </div>

    <!-- 加载态 -->
    <div v-if="loading" class="flex justify-center py-12">
      <NSpin size="medium" />
    </div>

    <!-- 错误态 -->
    <NAlert
      v-else-if="loadError"
      type="error"
      :title="loadError"
      class="mt-4"
    />

    <!-- 配置展示 -->
    <template v-else-if="config">
      <NDescriptions
        bordered
        :column="2"
        size="small"
        class="mt-4 config-table"
      >
        <!-- Provider -->
        <NDescriptionsItem label="Provider">
          <NSpace align="center" :size="4">
            <Plug :size="14" />
            <span>{{ providerLabel }}</span>
          </NSpace>
        </NDescriptionsItem>

        <NDescriptionsItem label="API 协议">
          <NSpace align="center" :size="4">
            <Globe :size="14" />
            <span>{{ config.protocol || "未配置" }}</span>
          </NSpace>
        </NDescriptionsItem>

        <!-- Model Name -->
        <NDescriptionsItem label="模型名称">
          <NSpace align="center" :size="4">
            <Brain :size="14" />
            <span>{{ config.model_name || "未配置" }}</span>
          </NSpace>
        </NDescriptionsItem>

        <!-- Base URL (sanitized) -->
        <NDescriptionsItem label="Base URL">
          <NSpace align="center" :size="4">
            <Globe :size="14" />
            <code class="text-xs">{{ config.base_url_display || "未配置" }}</code>
          </NSpace>
        </NDescriptionsItem>

        <!-- API Key Status -->
        <NDescriptionsItem label="API Key">
          <NSpace align="center" :size="4">
            <Key :size="14" />
            <NTag :type="apiKeyType" size="small" :bordered="false">
              {{ apiKeyStatus }}
            </NTag>
          </NSpace>
        </NDescriptionsItem>

        <!-- Timeout -->
        <NDescriptionsItem label="超时">
          <NSpace align="center" :size="4">
            <Clock :size="14" />
            <span>{{ config.timeout_seconds }}s</span>
          </NSpace>
        </NDescriptionsItem>

        <!-- Max Retries -->
        <NDescriptionsItem label="最大重试">
          <NSpace align="center" :size="4">
            <RefreshCw :size="14" />
            <span>{{ config.max_retries }} 次</span>
          </NSpace>
        </NDescriptionsItem>

        <!-- Max Tokens -->
        <NDescriptionsItem label="Max Tokens">
          <NSpace align="center" :size="4">
            <Zap :size="14" />
            <span>{{ config.max_tokens.toLocaleString() }}</span>
          </NSpace>
        </NDescriptionsItem>

        <!-- Thinking Mode -->
        <NDescriptionsItem label="Thinking Mode">
          <NTag
            :type="config.thinking_mode === 'disabled' ? 'default' : 'info'"
            size="small"
            :bordered="false"
          >
            {{ config.thinking_mode || "默认" }}
          </NTag>
        </NDescriptionsItem>
      </NDescriptions>

      <!-- Worker 状态 -->
      <div class="mt-4 p-3 rounded border border-[var(--color-border)]">
        <div class="flex items-center gap-2 text-sm font-medium text-[var(--color-text)]">
          <Activity :size="14" class="text-[var(--color-muted)]" />
          Worker 状态
        </div>
        <div class="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-[var(--color-muted)]">
          <span>
            状态：
            <NTag size="tiny" :bordered="false">
              {{ config.worker_status }}
            </NTag>
          </span>
          <span v-if="config.last_heartbeat_at">
            最近心跳：{{ new Date(config.last_heartbeat_at).toLocaleString() }}
          </span>
          <span v-if="config.last_error_code" class="text-[var(--color-danger)]">
            <AlertTriangle :size="12" class="inline" />
            最后错误：{{ config.last_error_code }}
          </span>
          <span v-if="!config.last_error_code && config.worker_status !== 'unknown'" class="text-[var(--color-success)]">
            <CheckCircle :size="12" class="inline" />
            无错误
          </span>
        </div>
      </div>

      <!-- 测试连接 -->
      <div class="mt-4 flex items-center gap-3">
        <NButton
          :loading="testing"
          :disabled="testButtonDisabled"
          @click="handleTest"
        >
          {{ testButtonLabel }}
        </NButton>
        <span class="text-xs text-[var(--color-muted)]">
          测试与模型 API 的连通性，结果写入审计日志
        </span>
      </div>

      <!-- 测试结果 -->
      <div v-if="testResult" class="mt-3">
        <NAlert
          :type="testResult.status === 'ok' ? 'success' : 'error'"
          :bordered="false"
        >
          <template #header>
            <div class="flex items-center gap-2">
              <CheckCircle v-if="testResult.status === 'ok'" :size="16" />
              <XCircle v-else :size="16" />
              <span>{{ testResult.status === 'ok' ? '连接成功' : '连接失败' }}</span>
            </div>
          </template>
          <div class="mt-1 text-xs space-y-0.5">
            <div>Provider: {{ testResult.provider }}</div>
            <div>Model: {{ testResult.model }}</div>
            <div>延迟: {{ testResult.latency_ms.toFixed(0) }}ms</div>
            <div>测试时间: {{ new Date(testResult.tested_at).toLocaleString() }}</div>
            <div v-if="testResult.error" class="mt-1">
              <NTag type="error" size="tiny" :bordered="false">
                {{ testResult.error.code }}
              </NTag>
              <span class="ml-1">{{ testResult.error.message }}</span>
            </div>
          </div>
        </NAlert>
      </div>
    </template>

    <!-- 空态：无配置 -->
    <div v-else class="mt-4 p-6 text-center border border-dashed border-[var(--color-border)] rounded">
      <p class="text-sm text-[var(--color-muted)]">无法获取模型配置信息</p>
    </div>
  </div>
</template>

<style scoped>
.model-config-panel {
  /* 遵循已有的密度风格 */
}

.panel-header {
  margin-bottom: 8px;
}

.config-table :deep(.n-descriptions-table-header) {
  width: 120px;
}
</style>
