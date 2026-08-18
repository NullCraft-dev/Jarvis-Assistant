// @vitest-environment happy-dom

import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MessageFeedback from "@/features/feedback/components/MessageFeedback.vue";

const apiMocks = vi.hoisted(() => ({ submitRagFeedback: vi.fn() }));
vi.mock("@/api/client", () => apiMocks);

describe("MessageFeedback", () => {
  beforeEach(() => apiMocks.submitRagFeedback.mockReset());

  it("submits structured answer feedback by persisted message id", async () => {
    apiMocks.submitRagFeedback.mockResolvedValue({ ok: true, data: { feedback: { kind: "unhelpful" } } });
    const wrapper = mount(MessageFeedback, { props: { messageId: "message-1", content: "回答" } });
    await wrapper.findAll("button").find((button) => button.text().includes("没帮助"))!.trigger("click");
    expect(apiMocks.submitRagFeedback).toHaveBeenCalledWith({ message_id: "message-1", kind: "unhelpful", citation_chunk_id: undefined });
    expect(wrapper.text()).toContain("将进入审核队列");
  });

  it("requires choosing a concrete citation chunk", async () => {
    apiMocks.submitRagFeedback.mockResolvedValue({ ok: true, data: { feedback: { kind: "citation_incorrect" } } });
    const chunkId = "11111111-1111-4111-8111-111111111111";
    const wrapper = mount(MessageFeedback, { props: { messageId: "message-1", content: `来源 [chunk:${chunkId}]` } });
    await wrapper.findAll("button").find((button) => button.text() === "引用有误")!.trigger("click");
    await wrapper.findAll("button").find((button) => button.text() === chunkId.slice(0, 8))!.trigger("click");
    expect(apiMocks.submitRagFeedback).toHaveBeenCalledWith({ message_id: "message-1", kind: "citation_incorrect", citation_chunk_id: chunkId });
  });
});
