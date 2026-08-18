// @vitest-environment happy-dom

import { nextTick, ref } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useTypewriterText } from "@/features/command/composables/useTypewriterText";

describe("useTypewriterText", () => {
  afterEach(() => vi.useRealTimers());

  it("reveals received runtime text progressively", async () => {
    vi.useFakeTimers();
    const target = ref("");
    const runId = ref<string | null>("run-1");
    const { displayedText, isTyping } = useTypewriterText(target, runId);

    target.value = "这是逐字显示的回复";
    await nextTick();
    expect(displayedText.value).toBe("");
    expect(isTyping.value).toBe(true);

    await vi.advanceTimersByTimeAsync(28);
    expect(displayedText.value).toBe("这");

    await vi.runAllTimersAsync();
    expect(displayedText.value).toBe(target.value);
    expect(isTyping.value).toBe(false);
  });

  it("resets the presentation buffer when the active run changes", async () => {
    vi.useFakeTimers();
    const target = ref("旧回复");
    const runId = ref<string | null>("run-1");
    const { displayedText } = useTypewriterText(target, runId);
    await vi.runAllTimersAsync();
    expect(displayedText.value).toBe("旧回复");

    target.value = "";
    runId.value = "run-2";
    await nextTick();
    expect(displayedText.value).toBe("");
  });

  it("shows restored persisted output immediately without replay", async () => {
    vi.useFakeTimers();
    const target = ref("已经持久化的完整历史回复");
    const runId = ref<string | null>("restored-run");
    const animate = ref(false);
    const { displayedText, isTyping } = useTypewriterText(target, runId, animate);

    await nextTick();

    expect(displayedText.value).toBe(target.value);
    expect(isTyping.value).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("can enable animation only for a locally started run", async () => {
    vi.useFakeTimers();
    const target = ref("历史回复");
    const runId = ref<string | null>("restored-run");
    const animate = ref(false);
    const { displayedText } = useTypewriterText(target, runId, animate);
    await nextTick();
    expect(displayedText.value).toBe("历史回复");

    target.value = "当前页面新任务的回复";
    runId.value = "local-run";
    animate.value = true;
    await nextTick();

    expect(displayedText.value).toBe("");
    await vi.advanceTimersByTimeAsync(28);
    expect(displayedText.value).toBe("当");
  });
});
