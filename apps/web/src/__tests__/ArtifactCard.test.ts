// @vitest-environment happy-dom

import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({ getArtifact: vi.fn() }));
vi.mock("@/api/client", () => apiMocks);

import ArtifactCard from "@/features/artifact/components/ArtifactCard.vue";

const artifact = {
  id: "artifact-1",
  task_id: "task-1",
  run_id: "run-1",
  kind: "markdown" as const,
  title: "最终回复",
  purpose: "deliverable" as const,
  producer: { type: "tool" as const, tool_call_id: "tool-call-1" },
  file_size_bytes: 12,
  mime_type: "text/markdown; charset=utf-8",
  content_hash: "abc",
  metadata: { storage: "local_file" },
  created_at: "2026-07-23T00:00:00Z",
};

describe("ArtifactCard", () => {
  beforeEach(() => {
    apiMocks.getArtifact.mockReset();
  });

  it("loads externalized content only when expanded", async () => {
    apiMocks.getArtifact.mockResolvedValue({
      ok: true,
      data: { ...artifact, content: "# 按需读取成功" },
    });
    const wrapper = mount(ArtifactCard, { props: { artifact } });

    expect(apiMocks.getArtifact).not.toHaveBeenCalled();
    const details = wrapper.get("details");
    (details.element as HTMLDetailsElement).open = true;
    await details.trigger("toggle");
    await flushPromises();

    expect(apiMocks.getArtifact).toHaveBeenCalledOnce();
    expect(wrapper.text()).toContain("按需读取成功");

    await details.trigger("toggle");
    await flushPromises();
    expect(apiMocks.getArtifact).toHaveBeenCalledOnce();
  });

  it("does not request inline content", async () => {
    const wrapper = mount(ArtifactCard, {
      props: { artifact: { ...artifact, content: "已内联" } },
    });
    const details = wrapper.get("details");
    (details.element as HTMLDetailsElement).open = true;
    await details.trigger("toggle");
    await flushPromises();
    expect(apiMocks.getArtifact).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("已内联");
  });

  it("loads a workspace file deliverable only when expanded", async () => {
    apiMocks.getArtifact.mockResolvedValue({
      ok: true,
      data: {
        ...artifact,
        kind: "file",
        content: "# workspace deliverable",
        metadata: {
          storage: "workspace",
          workspace_relative_path: "reports/result.md",
        },
      },
    });
    const wrapper = mount(ArtifactCard, {
      props: {
        artifact: {
          ...artifact,
          kind: "file",
          title: "reports/result.md",
          file_size_bytes: 2048,
          metadata: {
            storage: "workspace",
            workspace_relative_path: "reports/result.md",
          },
        },
      },
    });

    expect(apiMocks.getArtifact).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("工作区路径：reports/result.md");
    expect(wrapper.text()).toContain("2.0 KiB");
    const details = wrapper.get("details");
    (details.element as HTMLDetailsElement).open = true;
    await details.trigger("toggle");
    await flushPromises();

    expect(apiMocks.getArtifact).toHaveBeenCalledOnce();
    expect(wrapper.text()).toContain("workspace deliverable");
  });

  it("bounds a long title, path, and inline body", () => {
    const longToken = "segment".repeat(50);
    const wrapper = mount(ArtifactCard, {
      props: {
        artifact: {
          ...artifact,
          kind: "file",
          title: longToken,
          content: longToken,
          metadata: {
            storage: "workspace",
            workspace_relative_path: `reports/${longToken}.md`,
          },
        },
      },
    });

    expect(wrapper.get("details").classes()).toContain("overflow-hidden");
    expect(wrapper.get("summary span").attributes("title")).toBe(longToken);
    expect(wrapper.get("pre").classes()).toContain("max-w-full");
    expect(wrapper.text()).toContain(`reports/${longToken}.md`);
  });
});
