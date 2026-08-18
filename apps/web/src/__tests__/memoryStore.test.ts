// @vitest-environment happy-dom

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  listMemories: vi.fn(),
  listMemoryCandidates: vi.fn(),
  createMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  updateMemoryCandidate: vi.fn(),
  approveMemoryCandidate: vi.fn(),
  rejectMemoryCandidate: vi.fn(),
}));

vi.mock("@/api/client", () => ({ ...apiMocks }));

import { useMemoryStore } from "@/stores/memoryStore";

const candidate = {
  id: "candidate-1", scope_type: "global", category: "preference",
  suggested_key: "response.language", content: "使用中文", status: "pending",
  source_task_id: "task-1", source_run_id: "run-1", confidence: 0.9,
  importance: 80, sensitivity: "normal", extraction_policy_version: "v1",
  version: 1, created_at: "2026-07-26T00:00:00Z", updated_at: "2026-07-26T00:00:00Z",
} as const;

const memory = {
  id: "memory-1", scope_type: "global", category: "preference",
  key: "response.language", content: "使用中文", status: "active",
  source_type: "candidate_approved", importance: 80, version: 1,
  created_at: "2026-07-26T00:01:00Z", updated_at: "2026-07-26T00:01:00Z",
} as const;

describe("memoryStore candidate confirmation", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    apiMocks.listMemories.mockResolvedValue({ ok: true, data: { memories: [] } });
    apiMocks.listMemoryCandidates.mockResolvedValue({ ok: true, data: { candidates: [candidate] } });
  });

  it("loads pending candidates separately from confirmed memories", async () => {
    const store = useMemoryStore();
    await store.load();
    expect(store.memories).toEqual([]);
    expect(store.candidates).toEqual([candidate]);
    expect(apiMocks.listMemoryCandidates).toHaveBeenCalledWith("status=pending");
  });

  it("moves an approved candidate into confirmed memories", async () => {
    const store = useMemoryStore();
    store.candidates = [candidate];
    apiMocks.approveMemoryCandidate.mockResolvedValue({
      ok: true, data: { candidate: { ...candidate, status: "approved", version: 2 }, memory },
    });
    expect(await store.approveCandidate(candidate.id, { expected_version: 1 })).toBe(true);
    expect(store.candidates).toEqual([]);
    expect(store.memories).toEqual([memory]);
  });

  it("removes a rejected candidate without creating memory", async () => {
    const store = useMemoryStore();
    store.candidates = [candidate];
    apiMocks.rejectMemoryCandidate.mockResolvedValue({
      ok: true, data: { candidate: { ...candidate, status: "rejected", version: 2 } },
    });
    expect(await store.rejectCandidate(candidate.id, { expected_version: 1 })).toBe(true);
    expect(store.candidates).toEqual([]);
    expect(store.memories).toEqual([]);
  });
});
