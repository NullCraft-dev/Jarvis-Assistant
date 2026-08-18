// Permission Store — 管理权限请求
// 分层：Frontend State，只消费 DTO
// 真源：docs/08-permission-security-design.md, docs/13-interface-contract.md

import { defineStore } from "pinia";
import { ref, computed } from "vue";
import type {
  AppError,
  PermissionDecisionType,
  PermissionRequestDTO,
  PermissionDecisionDTO,
  ID,
} from "@jarvis/shared";
import * as api from "@/api/client";
import { normalizeClientError } from "@/api/errors";
import { useRunStore } from "@/stores/runStore";

export const usePermissionStore = defineStore("permission", () => {
  const pendingRequests = ref<PermissionRequestDTO[]>([]);
  const resolvedRequests = ref<PermissionRequestDTO[]>([]);
  const resolvingRequestIds = ref<Set<ID>>(new Set());
  const resolvingDecisions = ref<Map<ID, PermissionDecisionType>>(new Map());
  const errorsByRequestId = ref<Map<ID, AppError>>(new Map());
  let loadGeneration = 0;
  const MAX_RESOLVED_FEEDBACK = 50;

  const hasPending = computed(() => pendingRequests.value.length > 0);

  function addRequest(request: PermissionRequestDTO) {
    const expiresAt = Date.parse(request.expires_at);
    if (
      (request.status && request.status !== "pending") ||
      !Number.isFinite(expiresAt) ||
      expiresAt <= Date.now()
    ) {
      removeRequest(request.id);
      return;
    }
    const index = pendingRequests.value.findIndex((item) => item.id === request.id);
    if (index >= 0) {
      pendingRequests.value[index] = request;
    } else {
      pendingRequests.value.push(request);
    }
  }

  async function resolveRequest(decision: PermissionDecisionDTO) {
    if (resolvingRequestIds.value.has(decision.request_id)) return null;
    resolvingRequestIds.value = new Set(resolvingRequestIds.value).add(decision.request_id);
    resolvingDecisions.value.set(decision.request_id, decision.decision);
    errorsByRequestId.value.delete(decision.request_id);
    try {
      const result = await api.resolvePermission(decision);
      if (result.ok) {
        removeRequest(decision.request_id);
        resolvedRequests.value = [
          result.data.request,
          ...resolvedRequests.value.filter((item) => item.id !== decision.request_id),
        ].slice(0, MAX_RESOLVED_FEEDBACK);

        if (result.data.events && result.data.events.length > 0) {
          const runStore = useRunStore();
          for (const event of result.data.events) {
            runStore.appendEvent(event);
          }
        }
      } else {
        errorsByRequestId.value.set(decision.request_id, result.error);
      }
      return result;
    } catch (cause) {
      errorsByRequestId.value.set(
        decision.request_id,
        normalizeClientError(cause, "权限决定提交失败，请重试"),
      );
      return null;
    } finally {
      const next = new Set(resolvingRequestIds.value);
      next.delete(decision.request_id);
      resolvingRequestIds.value = next;
      resolvingDecisions.value.delete(decision.request_id);
    }
  }

  function removeRequest(requestId: ID) {
    pendingRequests.value = pendingRequests.value.filter((item) => item.id !== requestId);
    errorsByRequestId.value.delete(requestId);
  }

  function clearRun(runId: ID) {
    pendingRequests.value = pendingRequests.value.filter((item) => item.run_id !== runId);
  }

  function getPendingByRun(runId: ID) {
    return pendingRequests.value.filter((item) => item.run_id === runId);
  }

  function isResolving(requestId: ID) {
    return resolvingRequestIds.value.has(requestId);
  }

  function getError(requestId: ID) {
    return errorsByRequestId.value.get(requestId) ?? null;
  }

  function clearError(requestId: ID) {
    errorsByRequestId.value.delete(requestId);
  }

  function getResolvingDecision(requestId: ID) {
    return resolvingDecisions.value.get(requestId) ?? null;
  }

  function getResolvedByRun(runId: ID) {
    return resolvedRequests.value.filter((item) => item.run_id === runId);
  }

  async function loadPendingForRun(runId: ID) {
    const generation = ++loadGeneration;
    const result = await api.listPendingPermissions(runId);
    if (generation !== loadGeneration) return result;
    if (result.ok) {
      clearRun(runId);
      for (const request of result.data.requests) addRequest(request);
    }
    return result;
  }

  function denyRequest(requestId: ID) {
    return resolveRequest({ request_id: requestId, decision: "deny" });
  }

  function approveOnce(requestId: ID) {
    return resolveRequest({
      request_id: requestId,
      decision: "allow_once",
    });
  }

  return {
    pendingRequests,
    resolvedRequests,
    hasPending,
    resolvingRequestIds,
    resolvingDecisions,
    errorsByRequestId,
    addRequest,
    removeRequest,
    clearRun,
    getPendingByRun,
    isResolving,
    getError,
    clearError,
    getResolvingDecision,
    getResolvedByRun,
    loadPendingForRun,
    resolveRequest,
    denyRequest,
    approveOnce,
  };
});
