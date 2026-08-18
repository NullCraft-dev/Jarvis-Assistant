// @vitest-environment happy-dom

import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";
import type { RagDocumentDTO, WorkspaceDTO } from "@jarvis/shared";

import RagDocumentLibrary from "@/features/knowledge/components/RagDocumentLibrary.vue";
import { useRagDocumentStore } from "@/stores/ragDocumentStore";
import { useWorkspaceStore } from "@/stores/workspaceStore";

describe("RagDocumentLibrary", () => {
  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
  });

  it("shows trusted source, versions, counts, and recent job details", async () => {
    const wrapper = mountLibrary([document("paper")]);

    expect(wrapper.text()).toContain("受控 Artifact · artifact-paper");
    expect(wrapper.text()).toContain("文档版本：v4");
    expect(wrapper.text()).toContain("索引版本：rag-v2");
    expect(wrapper.text()).toContain("分块计数：8");
    expect(wrapper.text()).toContain("向量计数：8/8");

    await wrapper.get("summary").trigger("click");
    expect(wrapper.text()).toContain("Parser：pymupdf-v2");
    expect(wrapper.text()).toContain("最近 Job ID：job-paper");
  });

  it("previews eligible and skipped counts before a bounded batch action", async () => {
    const wrapper = mountLibrary([
      document("ready", { status: "ready" }),
      document("failed", { status: "failed" }),
    ]);
    const checkboxes = wrapper.findAll('input[type="checkbox"]');
    await checkboxes[0]!.setValue(true);
    await checkboxes[1]!.setValue(true);
    const batchDisable = wrapper.findAll("button").find((button) => button.text() === "批量停用");
    await batchDisable!.trigger("click");

    expect(wrapper.get('[role="dialog"]').text()).toContain("停止选中文档参与检索");
    expect(wrapper.get('[role="dialog"]').text()).toContain("将执行1");
    expect(wrapper.get('[role="dialog"]').text()).toContain("状态不适用，将跳过1");
  });

  it("makes the per-document L4 boundary explicit before batch delete", async () => {
    const wrapper = mountLibrary([document("paper")]);
    await wrapper.get('input[type="checkbox"]').setValue(true);
    const batchDelete = wrapper.findAll("button").find((button) => button.text() === "批量删除");
    await batchDelete!.trigger("click");

    expect(wrapper.get('[role="dialog"]').text()).toContain("每个文档都必须单独通过 L4 单次确认");
    expect(wrapper.get('[role="dialog"]').text()).toContain("逐项确认或跳过");
  });

  it("shows the exact L2 upload scope before any document is created", async () => {
    const wrapper = mountLibrary([]);
    const store = useRagDocumentStore();
    store.uploadRequest = {
      id: "upload-request",
      task_id: "task",
      run_id: "run",
      tool_name: "rag.upload_pdf",
      action_summary: "将 paper.pdf 加入 RAG 文档库",
      risk_level: "L2",
      scope: { type: "once" },
      arguments_summary: {
        filename: "paper.pdf",
        size_bytes: 1024,
        content_sha256: "a".repeat(64),
      },
      allowed_decisions: ["allow_once", "deny"],
      created_at: "2026-08-05T00:00:00Z",
      expires_at: "2099-08-05T00:15:00Z",
      status: "pending",
    };
    await nextTick();

    const dialog = wrapper.get('[role="dialog"]');
    expect(dialog.text()).toContain("风险等级：L2");
    expect(dialog.text()).toContain("rag.upload_pdf");
    expect(dialog.text()).toContain("paper.pdf");
    expect(dialog.text()).toContain("拒绝不会产生这些副作用");
  });
});

function mountLibrary(documents: RagDocumentDTO[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const workspaceStore = useWorkspaceStore();
  const workspace: WorkspaceDTO = {
    id: "workspace",
    name: "Jarvis",
    root_path: "/workspace",
    canonical_path: "/workspace",
    status: "active",
    source: "configured",
    created_at: "2026-07-31T00:00:00Z",
    updated_at: "2026-07-31T00:00:00Z",
  };
  workspaceStore.workspaces = [workspace];
  workspaceStore.setSelectedWorkspaceId(workspace.id);
  const ragStore = useRagDocumentStore();
  ragStore.documents = documents;
  ragStore.loadedWorkspaceId = workspace.id;
  return mount(RagDocumentLibrary, { global: { plugins: [pinia] } });
}

function document(id: string, overrides: Partial<RagDocumentDTO> = {}): RagDocumentDTO {
  return {
    id,
    workspace_id: "workspace",
    source_artifact_id: `artifact-${id}`,
    title: `${id}.pdf`,
    mime_type: "application/pdf",
    status: "ready",
    ingestion_policy_version: "rag-v2",
    parser_version: "pymupdf-v2",
    chunker_version: "structure-v2",
    embedding_provider: "openai",
    embedding_model: "text-embedding-3-small",
    embedding_dimensions: 1536,
    chunk_count: 8,
    indexed_at: "2026-07-31T00:01:00Z",
    version: 4,
    created_at: "2026-07-31T00:00:00Z",
    updated_at: "2026-07-31T00:02:00Z",
    latest_job: {
      id: `job-${id}`,
      status: "completed",
      attempts: 1,
      max_attempts: 3,
      embedding_attempts: 1,
      embedding_max_attempts: 3,
      progress: {
        page_count: 2,
        native_extraction_done: true,
        visual_pages_total: 0,
        visual_pages_completed: 0,
        visual_route_counts: {},
        chunks_total: 8,
        embedding_total: 8,
        embedding_completed: 8,
      },
      started_at: "2026-07-31T00:00:10Z",
      completed_at: "2026-07-31T00:01:00Z",
      created_at: "2026-07-31T00:00:00Z",
      updated_at: "2026-07-31T00:01:00Z",
    },
    index_state: "current",
    index_stale_reasons: [],
    index_target: { ingestion_policy_version: "rag-v2" },
    ...overrides,
  };
}
