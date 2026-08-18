// @vitest-environment happy-dom

import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceDTO } from "@jarvis/shared";

const apiMocks = vi.hoisted(() => ({
  listRagEvaluationTraces: vi.fn(),
  inspectRagEvaluationTrace: vi.fn(),
  reviewRagEvaluationPrivacy: vi.fn(),
  reviewRagEvaluationLabel: vi.fn(),
  promoteRagEvaluationTrace: vi.fn(),
}));
vi.mock("@/api/client", () => apiMocks);

import RagEvaluationReviewConsole from "@/features/knowledge/components/RagEvaluationReviewConsole.vue";
import { useWorkspaceStore } from "@/stores/workspaceStore";

const trace = {
  trace_id: "trace-1",
  workspace_id: "workspace-1",
  query_hash: "0123456789abcdef",
  pipeline_version: "rag-v1",
  privacy_status: "pending",
  label_status: null,
  candidate_count: 12,
  reranked_count: 8,
  context_chunk_count: 4,
  context_truncated: false,
  created_at: "2026-08-02T00:00:00Z",
};
const detail = {
  trace,
  query: null,
  request: {},
  evidence: [],
  label: null,
  promotion_candidate: null,
};

describe("RagEvaluationReviewConsole", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    apiMocks.listRagEvaluationTraces.mockResolvedValue({ ok: true, data: { traces: [trace] } });
    apiMocks.inspectRagEvaluationTrace.mockResolvedValue({ ok: true, data: detail });
  });

  it("opens the selected review in a modal instead of appending it below the queue", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const workspaceStore = useWorkspaceStore();
    const workspace: WorkspaceDTO = {
      id: "workspace-1",
      name: "Jarvis",
      root_path: "/workspace",
      canonical_path: "/workspace",
      status: "active",
      source: "configured",
      created_at: "2026-08-02T00:00:00Z",
      updated_at: "2026-08-02T00:00:00Z",
    };
    workspaceStore.workspaces = [workspace];
    workspaceStore.setSelectedWorkspaceId(workspace.id);

    const wrapper = mount(RagEvaluationReviewConsole, {
      attachTo: document.body,
      global: { plugins: [pinia] },
    });
    await flushPromises();

    const reviewButton = wrapper.findAll("button").find((button) => button.text() === "审核");
    expect(reviewButton).toBeDefined();
    await reviewButton!.trigger("click");
    await flushPromises();

    expect(apiMocks.inspectRagEvaluationTrace).toHaveBeenCalledWith("workspace-1", "trace-1");
    const dialog = document.body.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain("审核轨迹 trace-1");
    expect(dialog?.textContent).toContain("批准隐私");
    expect(wrapper.text()).not.toContain("审核轨迹 trace-1");

    const closeButton = dialog?.querySelector('button[aria-label="关闭审核弹窗"]') as HTMLButtonElement;
    closeButton.click();
    await flushPromises();
    expect(document.body.querySelector('[role="dialog"]')).toBeNull();
    wrapper.unmount();
  });

  it("opens a gate failure target with its authoritative workspace scope", async () => {
    const pinia = createPinia(); setActivePinia(pinia);
    apiMocks.listRagEvaluationTraces.mockResolvedValueOnce({ ok: true, data: { traces: [] } });
    const wrapper = mount(RagEvaluationReviewConsole, {
      attachTo: document.body, global: { plugins: [pinia] },
      props: { reviewTarget: {
        candidate_id: "b".repeat(64), trace_id: "trace-1", workspace_id: "workspace-1",
        query_hash: "a".repeat(64), failure_type: "candidate_evidence_missed",
        suspected_stage: "candidate", severity: "high", metric_ids: ["candidate.recall@5"],
        privacy_status: "pending", label_status: null, label_source: null,
        review_state: "privacy_required",
        issue: null,
      } },
    });
    await flushPromises();
    expect(apiMocks.inspectRagEvaluationTrace).toHaveBeenCalledWith("workspace-1", "trace-1");
    expect(Object.keys(wrapper.emitted())).toContain("targetOpened");
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain("审核轨迹 trace-1");
    wrapper.unmount();
  });
});
