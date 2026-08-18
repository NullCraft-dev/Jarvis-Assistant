// Run Store — 管理 RuntimeEvent 流和运行状态
// 前端不猜测任务结论，只从 RuntimeEvent 推导 UI 状态
// 真源：docs/13-interface-contract.md, docs/11-frontend-app-ui-design.md

import { defineStore } from "pinia";
import { ref } from "vue";
import type { RuntimeEvent, AgentRunStatus, ID, PermissionRequestDTO } from "@jarvis/shared";
import * as api from "@/api/client";
import type { EventConnectionState } from "@/api/transport";
import { usePermissionStore } from "@/stores/permissionStore";
import { useUiStore } from "@/stores/uiStore";

const TERMINAL_RUN_STATUSES = new Set<AgentRunStatus>(["completed", "failed", "cancelled"]);

export const useRunStore = defineStore("run", () => {
  const eventsByRunId = ref<Map<ID, RuntimeEvent[]>>(new Map());
  const runStatus = ref<Map<ID, AgentRunStatus>>(new Map());
  const streamingText = ref<Map<ID, string>>(new Map());
  /** agent.run.completed 的 payload.output（非 streaming 模式下的 assistant 回复） */
  const finalOutputText = ref<Map<ID, string>>(new Map());
  const unsubscribers = ref<Map<ID, () => void>>(new Map());
  const connectionState = ref<Map<ID, EventConnectionState>>(new Map());
  /** 已投影到 UI 状态的最新 PostgreSQL durable sequence。 */
  const lastProjectedSequence = ref<Map<ID, number>>(new Map());

  function getEvents(runId: ID): RuntimeEvent[] {
    return eventsByRunId.value.get(runId) ?? [];
  }

  function getStatus(runId: ID): AgentRunStatus {
    return runStatus.value.get(runId) ?? "created";
  }

  function getStreamingText(runId: ID): string {
    return streamingText.value.get(runId) ?? "";
  }

  function getConnectionState(runId: ID): EventConnectionState {
    return connectionState.value.get(runId) ?? "closed";
  }

  /** 获取 agent.run.completed 的最终输出文本（非 streaming 模式） */
  function getFinalOutputText(runId: ID): string {
    return finalOutputText.value.get(runId) ?? "";
  }

  function appendEvent(event: RuntimeEvent) {
    const runId = event.run_id;
    if (!runId) return;

    // 按 event.id 去重：避免 SSE 重推 + resolve 手动追加造成重复
    const existing = eventsByRunId.value.get(runId) ?? [];
    const ids = new Set(existing.map((e) => e.id));
    if (ids.has(event.id)) return; // 重复事件，跳过

    // Control Plane 接受权限决定后会随 POST 响应立即返回 acknowledgement，
    // Worker 在恢复执行收口时还会发布 durable permission.resolved。两者 event.id
    // 可以不同，但 request_id 指向同一个用户决定，前端只投影一次。
    if (event.type === "permission.resolved") {
      const requestId = (event.payload as { request_id?: ID })?.request_id;
      const alreadyResolved = requestId && existing.some((item) =>
        item.type === "permission.resolved" &&
        (item.payload as { request_id?: ID })?.request_id === requestId
      );
      if (alreadyResolved) return;
    }

    existing.push(event);
    eventsByRunId.value.set(runId, existing);

    // 迟到事件仍保留在 Timeline/诊断事件中，但终态是 UI 状态栅栏，不能被
    // started/resumed/permission/model.delta 再次打开。Gateway 已执行相同约束，
    // 这里是客户端最后一道防线。
    if (TERMINAL_RUN_STATUSES.has(getStatus(runId))) return;

    // sequence 只存在于 PostgreSQL durable 历史。只在两条 durable 事件之间
    // 比较，不能用它丢弃没有 sequence 的实时 delta。
    if (typeof event.sequence === "number") {
      const previousSequence = lastProjectedSequence.value.get(runId);
      if (previousSequence !== undefined && event.sequence <= previousSequence) return;
      lastProjectedSequence.value.set(runId, event.sequence);
    }

    // 根据事件类型更新运行状态
    switch (event.type) {
      case "agent.run.started":
        runStatus.value.set(runId, "running");
        break;
      case "agent.run.paused":
        runStatus.value.set(runId, "paused");
        break;
      case "agent.run.resumed":
        runStatus.value.set(runId, "running");
        break;
      case "agent.run.completed": {
        runStatus.value.set(runId, "completed");
        clearPendingPermissions(runId);
        // 非 streaming 模式：从 payload.output 读取 assistant 最终回复
        const p = event.payload as { output?: unknown };
        if (typeof p?.output === "string" && p.output.trim()) {
          finalOutputText.value.set(runId, p.output);
        }
        break;
      }
      case "agent.run.failed":
        runStatus.value.set(runId, "failed");
        clearPendingPermissions(runId);
        break;
      case "agent.run.cancelled":
        runStatus.value.set(runId, "cancelled");
        clearPendingPermissions(runId);
        break;
      case "permission.required": {
        runStatus.value.set(runId, "waiting_for_permission");
        // 将权限请求写入 permissionStore
        const payload = event.payload as { request?: PermissionRequestDTO };
        if (payload.request) {
          const permStore = usePermissionStore();
          permStore.addRequest(payload.request);
          // 自动切换到权限 tab
          const ui = useUiStore();
          ui.setInspectorTab("permissions");
        }
        break;
      }
      case "permission.resolved": {
        const payload = event.payload as { request_id?: ID; decision?: string };
        if (payload.request_id) {
          usePermissionStore().removeRequest(payload.request_id);
        }
        if (payload.decision && payload.decision !== "deny") {
          runStatus.value.set(runId, "running");
        }
        break;
      }
      case "permission.expired": {
        const payload = event.payload as { request_id?: ID };
        if (payload.request_id) {
          usePermissionStore().removeRequest(payload.request_id);
        }
        break;
      }
      case "model.delta": {
        const payload = event.payload as { delta?: string; accumulated?: string };
        // 生产流式事件只发送有界 delta，避免重复携带不断增长的 accumulated。
        // 保留 accumulated 兼容旧 mock / 历史事件。
        const nextText = typeof payload.accumulated === "string"
          ? payload.accumulated
          : (streamingText.value.get(runId) ?? "") + (payload.delta ?? "");
        streamingText.value.set(runId, nextText);
        break;
      }
    }

    // terminal event 到达后主动关闭该 run 的 EventSource，避免连接堆积
    // terminal event 类型：agent.run.completed、agent.run.failed、agent.run.cancelled
    if (
      event.type === "agent.run.completed" ||
      event.type === "agent.run.failed" ||
      event.type === "agent.run.cancelled"
    ) {
      unsubscribe(runId);
    }
  }

  /** 清理指定 run 的待处理权限请求（terminal event 到达后调用） */
  function clearPendingPermissions(runId: ID) {
    usePermissionStore().clearRun(runId);
  }

  function subscribe(runId: ID) {
    // 避免重复订阅
    if (unsubscribers.value.has(runId)) return;

    const unsub = api.subscribeRunEvents(
      runId,
      (event) => {
        appendEvent(event);
      },
      (state) => {
        connectionState.value.set(runId, state);
      },
    );
    unsubscribers.value.set(runId, unsub);
  }

  function unsubscribe(runId: ID) {
    const unsub = unsubscribers.value.get(runId);
    if (unsub) {
      unsub();
      unsubscribers.value.delete(runId);
    }
  }

  function resubscribe(runId: ID) {
    unsubscribe(runId);
    subscribe(runId);
  }

  function clearRun(runId: ID) {
    unsubscribe(runId);
    eventsByRunId.value.delete(runId);
    runStatus.value.delete(runId);
    streamingText.value.delete(runId);
    finalOutputText.value.delete(runId);
    connectionState.value.delete(runId);
    lastProjectedSequence.value.delete(runId);
    usePermissionStore().clearRun(runId);
  }

  return {
    eventsByRunId,
    runStatus,
    streamingText,
    finalOutputText,
    connectionState,
    getEvents,
    getStatus,
    getStreamingText,
    getFinalOutputText,
    getConnectionState,
    appendEvent,
    subscribe,
    resubscribe,
    unsubscribe,
    clearRun,
  };
});
