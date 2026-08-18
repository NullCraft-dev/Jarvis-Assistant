// @vitest-environment happy-dom

import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RagQualityGateHistory from "@/features/knowledge/components/RagQualityGateHistory.vue";

const apiMocks = vi.hoisted(() => ({ listRagQualityGateRuns: vi.fn(), listRagQualityFailureTargets: vi.fn(), updateRagQualityIssue: vi.fn() }));
vi.mock("@/api/client", () => apiMocks);

describe("RagQualityGateHistory", () => {
  beforeEach(() => {
    apiMocks.listRagQualityGateRuns.mockReset();
    apiMocks.listRagQualityFailureTargets.mockReset();
    apiMocks.updateRagQualityIssue.mockReset();
    apiMocks.listRagQualityFailureTargets.mockResolvedValue({ ok: true, data: { targets: [{
      candidate_id: "b".repeat(64), trace_id: "22222222-2222-4222-8222-222222222222",
      workspace_id: "33333333-3333-4333-8333-333333333333", query_hash: "a".repeat(64),
      failure_type: "candidate_evidence_missed", suspected_stage: "candidate", severity: "high",
      metric_ids: ["candidate.recall@5"], privacy_status: "approved", label_status: "promoted",
      label_source: "human_review", review_state: "fixed_regression_sample",
      issue: { id: "44444444-4444-4444-8444-444444444444", candidate_id: "b".repeat(64), trace_id: "22222222-2222-4222-8222-222222222222", gate_id: "rag-promoted-release-v1", cohort_id: "rag-promoted-p4-v1", failure_type: "candidate_evidence_missed", owner: "candidate_recall", status: "open", occurrence_count: 1, first_seen_run_id: "11111111-1111-4111-8111-111111111111", last_seen_run_id: "11111111-1111-4111-8111-111111111111", verified_run_id: null, resolution_note: "", version: 1, created_at: "2026-08-02T00:00:00Z", updated_at: "2026-08-02T00:00:00Z" },
    }] } });
    apiMocks.updateRagQualityIssue.mockResolvedValue({ ok: true, data: { issue: { id: "44444444-4444-4444-8444-444444444444", candidate_id: "b".repeat(64), trace_id: "22222222-2222-4222-8222-222222222222", gate_id: "rag-promoted-release-v1", cohort_id: "rag-promoted-p4-v1", failure_type: "candidate_evidence_missed", owner: "candidate_recall", status: "in_progress", occurrence_count: 1, first_seen_run_id: "11111111-1111-4111-8111-111111111111", last_seen_run_id: "11111111-1111-4111-8111-111111111111", verified_run_id: null, resolution_note: "", version: 2, created_at: "2026-08-02T00:00:00Z", updated_at: "2026-08-02T00:01:00Z" } } });
    apiMocks.listRagQualityGateRuns.mockResolvedValue({
      ok: true,
      data: {
        runs: [{
          id: "11111111-1111-4111-8111-111111111111",
          gate_id: "rag-promoted-release-v1",
          cohort_id: "rag-promoted-p4-v1",
          baseline_id: "rag-promoted-p4-v1",
          revision: "a".repeat(40),
          status: "passed",
          sample_count: 10,
          metrics: { "candidate.recall@5": 0.9, "context.evidence_recall": 1 },
          checks: [{ check_id: "minimum_sample_count", passed: true, actual: 10, required: 10 }],
          generated_at: "2026-08-02T08:00:00+00:00",
        }],
        insights: {
          comparison_state: "insufficient_history",
          compatible_history_count: 1,
          previous_run_id: null,
          metric_trends: [],
          alerts: [],
          failure_clusters: [{
            failure_type: "candidate_evidence_missed",
            priority: "medium",
            latest_rate: 0.2,
            latest_count: 2,
            previous_rate: null,
            rate_delta: null,
            occurrence_count: 1,
            threshold: 0.35,
            check_passed: true,
          }],
        },
      },
    });
  });

  it("只读展示最新门禁、脱敏版本和指标", async () => {
    const wrapper = mount(RagQualityGateHistory);
    await flushPromises();

    expect(wrapper.text()).toContain("最新结果：已通过");
    expect(wrapper.text()).toContain("候选召回@5");
    expect(wrapper.text()).toContain("90.0%");
    expect(wrapper.text()).toContain("rag-promoted-p4-v1");
    expect(wrapper.text()).toContain("当前只有一次可比运行");
    expect(wrapper.text()).toContain("候选证据漏召回");
    expect(wrapper.text()).toContain("20.0%");
    expect(wrapper.text()).not.toContain("执行门禁");
    expect(wrapper.text()).toContain("查看样本");
    expect(apiMocks.listRagQualityGateRuns).toHaveBeenCalledWith(20);
  });

  it("按需定位脱敏失败样本并交给现有审核台", async () => {
    const wrapper = mount(RagQualityGateHistory);
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text() === "查看样本")!.trigger("click");
    await flushPromises();
    expect(apiMocks.listRagQualityFailureTargets).toHaveBeenCalledWith("11111111-1111-4111-8111-111111111111", "candidate_evidence_missed");
    expect(wrapper.text()).toContain("已在固定回归集");
    expect(wrapper.text()).toContain("待处理");
    await wrapper.findAll("button").find((button) => button.text() === "打开审核")!.trigger("click");
    expect(wrapper.emitted("reviewTarget")?.[0]?.[0]).toMatchObject({ trace_id: "22222222-2222-4222-8222-222222222222" });
  });

  it("用乐观版本推进质量问题治理状态", async () => {
    const wrapper = mount(RagQualityGateHistory); await flushPromises();
    await wrapper.findAll("button").find((button) => button.text() === "查看样本")!.trigger("click"); await flushPromises();
    await wrapper.findAll("button").find((button) => button.text() === "开始处理")!.trigger("click"); await flushPromises();
    expect(apiMocks.updateRagQualityIssue).toHaveBeenCalledWith("44444444-4444-4444-8444-444444444444", { expected_version: 1, owner: "candidate_recall", status: "in_progress", resolution_note: "" });
    expect(wrapper.text()).toContain("处理中");
  });

  it("展示后端判定的退化趋势和提醒", async () => {
    apiMocks.listRagQualityGateRuns.mockResolvedValueOnce({
      ok: true,
      data: {
        runs: [{
          id: "11111111-1111-4111-8111-111111111111",
          gate_id: "rag-promoted-release-v1",
          cohort_id: "rag-promoted-p4-v1",
          baseline_id: "rag-promoted-p4-v1",
          revision: "b".repeat(40),
          status: "passed",
          sample_count: 10,
          metrics: { "candidate.recall@5": 0.85 },
          checks: [],
          generated_at: "2026-08-02T09:00:00+00:00",
        }],
        insights: {
          comparison_state: "ready",
          compatible_history_count: 2,
          previous_run_id: "22222222-2222-4222-8222-222222222222",
          metric_trends: [{ metric_id: "candidate.recall@5", current: 0.85, previous: 0.9, delta: -0.05, direction: "regressed" }],
          alerts: [{ code: "metric_regressed", severity: "warning", subject_id: "candidate.recall@5", current: 0.85, previous: 0.9, delta: -0.05 }],
          failure_clusters: [],
        },
      },
    });

    const wrapper = mount(RagQualityGateHistory);
    await flushPromises();

    expect(wrapper.text()).toContain("2 次可比运行");
    expect(wrapper.text()).toContain("退化 −5.0%");
    expect(wrapper.text()).toContain("指标较上次退化：候选召回@5");
  });
});
