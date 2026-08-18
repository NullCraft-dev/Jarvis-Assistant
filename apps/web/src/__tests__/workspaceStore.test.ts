// @vitest-environment happy-dom

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkspaceDTO } from "@jarvis/shared";

const apiMocks = vi.hoisted(() => ({
  listWorkspaces: vi.fn(),
  pickWorkspace: vi.fn(),
  revokeWorkspace: vi.fn(),
}));

vi.mock("@/api/client", () => ({ ...apiMocks }));

import { useWorkspaceStore } from "@/stores/workspaceStore";

function workspace(id: string, overrides: Partial<WorkspaceDTO> = {}): WorkspaceDTO {
  return {
    id,
    name: `project-${id}`,
    root_path: `/workspaces/${id}`,
    canonical_path: `/workspaces/${id}`,
    status: "active",
    source: "user_picker",
    created_at: "2026-07-16T00:00:00Z",
    updated_at: "2026-07-16T00:00:00Z",
    ...overrides,
  };
}

describe("workspaceStore", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("selects the first active workspace on a fresh load", async () => {
    apiMocks.listWorkspaces.mockResolvedValue({ ok: true, data: { workspaces: [workspace("one"), workspace("two")] } });
    const store = useWorkspaceStore();
    await store.loadWorkspaces();
    expect(store.selectedWorkspaceId).toBe("one");
    expect(localStorage.getItem("jarvis_selected_workspace_id")).toBe("one");
  });

  it("preserves an active saved selection", async () => {
    localStorage.setItem("jarvis_selected_workspace_id", "two");
    setActivePinia(createPinia());
    apiMocks.listWorkspaces.mockResolvedValue({ ok: true, data: { workspaces: [workspace("one"), workspace("two")] } });
    const store = useWorkspaceStore();
    await store.loadWorkspaces();
    expect(store.selectedWorkspaceId).toBe("two");
  });

  it("replaces a revoked saved selection", async () => {
    localStorage.setItem("jarvis_selected_workspace_id", "old");
    setActivePinia(createPinia());
    apiMocks.listWorkspaces.mockResolvedValue({
      ok: true,
      data: { workspaces: [workspace("old", { status: "revoked" }), workspace("active")] },
    });
    const store = useWorkspaceStore();
    await store.loadWorkspaces();
    expect(store.selectedWorkspaceId).toBe("active");
  });

  it("keeps an empty registry unselected", async () => {
    apiMocks.listWorkspaces.mockResolvedValue({ ok: true, data: { workspaces: [] } });
    const store = useWorkspaceStore();
    await store.loadWorkspaces();
    expect(store.selectedWorkspaceId).toBeNull();
  });

  it("treats picker cancellation as a non-error", async () => {
    apiMocks.pickWorkspace.mockResolvedValue({ ok: true, data: { workspace: null, cancelled: true } });
    const store = useWorkspaceStore();
    await expect(store.pickAndAddWorkspace()).resolves.toEqual({ cancelled: true });
    expect(store.error).toBeNull();
  });

  it("merges and selects the authoritative picker result", async () => {
    const added = workspace("added");
    apiMocks.pickWorkspace.mockResolvedValue({ ok: true, data: { workspace: added, cancelled: false } });
    apiMocks.listWorkspaces.mockResolvedValue({ ok: true, data: { workspaces: [added] } });
    const store = useWorkspaceStore();
    await store.pickAndAddWorkspace();
    expect(store.workspaces).toContainEqual(added);
    expect(store.selectedWorkspaceId).toBe("added");
  });

  it("ignores an older list response", async () => {
    let resolveOld!: (value: unknown) => void;
    const oldResponse = new Promise((resolve) => { resolveOld = resolve; });
    apiMocks.listWorkspaces
      .mockReturnValueOnce(oldResponse)
      .mockResolvedValueOnce({ ok: true, data: { workspaces: [workspace("new")] } });
    const store = useWorkspaceStore();
    const first = store.loadWorkspaces();
    await store.loadWorkspaces();
    resolveOld({ ok: true, data: { workspaces: [workspace("old")] } });
    await first;
    expect(store.workspaces.map((ws) => ws.id)).toEqual(["new"]);
  });

  it("selects another workspace after revoking the current one", async () => {
    const current = workspace("current");
    const next = workspace("next");
    const store = useWorkspaceStore();
    store.workspaces = [current, next];
    store.setSelectedWorkspaceId(current.id);
    apiMocks.revokeWorkspace.mockResolvedValue({
      ok: true,
      data: { workspace: { ...current, status: "revoked" } },
    });
    apiMocks.listWorkspaces.mockResolvedValue({ ok: true, data: { workspaces: [next] } });
    await store.revokeWorkspace(current.id);
    expect(store.selectedWorkspaceId).toBe(next.id);
  });
});
