// @vitest-environment happy-dom

import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RagQualityIssueLedger from "@/features/knowledge/components/RagQualityIssueLedger.vue";

const apiMocks = vi.hoisted(() => ({ listRagQualityIssues: vi.fn(), updateRagQualityIssue: vi.fn() }));
vi.mock("@/api/client", () => apiMocks);

const issue = {
  id: "44444444-4444-4444-8444-444444444444", candidate_id: "b".repeat(64),
  trace_id: "22222222-2222-4222-8222-222222222222", gate_id: "gate-v1", cohort_id: "cohort-v1",
  failure_type: "candidate_evidence_missed", owner: "candidate_recall", status: "verified",
  occurrence_count: 2, first_seen_run_id: "11111111-1111-4111-8111-111111111111",
  last_seen_run_id: "11111111-1111-4111-8111-111111111111", verified_run_id: "55555555-5555-4555-8555-555555555555",
  resolution_note: "修复候选过滤", version: 3, created_at: "2026-08-02T00:00:00Z", updated_at: "2026-08-02T01:00:00Z",
};

describe("RagQualityIssueLedger", () => {
  beforeEach(() => {
    apiMocks.listRagQualityIssues.mockReset(); apiMocks.updateRagQualityIssue.mockReset();
    apiMocks.listRagQualityIssues.mockResolvedValue({ ok: true, data: { issues: [{
      issue: { ...issue }, workspace_id: "33333333-3333-4333-8333-333333333333",
      trace_id: issue.trace_id, query_hash: "a".repeat(64), privacy_status: "approved", label_status: "promoted",
      review_state: "fixed_regression_sample", first_seen_revision: "a".repeat(40),
      last_seen_revision: "b".repeat(40), verified_revision: "c".repeat(40),
    }], summary: { total: 3, open: 1, in_progress: 0, resolved: 0, verified: 2, dismissed: 0 } } });
  });

  it("独立展示已验证问题、版本轨迹和全局统计", async () => {
    const wrapper = mount(RagQualityIssueLedger); await flushPromises();
    expect(apiMocks.listRagQualityIssues).toHaveBeenCalledWith("all", "all", "all", 50);
    expect(wrapper.text()).toContain("质量问题台账");
    expect(wrapper.text()).toContain("候选证据漏召回");
    expect(wrapper.text()).toContain("修复候选过滤");
    expect(wrapper.text()).toContain("aaaaaaaa / bbbbbbbb");
    expect(wrapper.text()).toContain("cccccccc");
    expect(wrapper.text()).toContain("记录版本 v3");
  });

  it("可从台账重新打开原审核轨迹", async () => {
    const wrapper = mount(RagQualityIssueLedger); await flushPromises();
    await wrapper.findAll("button").find((button) => button.text() === "打开审核")!.trigger("click");
    expect(wrapper.emitted("reviewTarget")?.[0]?.[0]).toEqual({ trace_id: issue.trace_id, workspace_id: "33333333-3333-4333-8333-333333333333" });
  });
});
