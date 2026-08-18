// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from "vitest";
import { apiPost, subscribeEvents } from "@/api/transport";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("API transport recovery", () => {
  it("normalizes a fetch rejection into a recoverable AppError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    await expect(apiPost("/tasks", { user_goal: "test" })).resolves.toEqual({
      ok: false,
      error: {
        code: "NETWORK_UNAVAILABLE",
        message: "无法连接 Jarvis 服务，请检查服务状态后重试",
        category: "internal",
        recoverable: true,
      },
    });
  });

  it("does not mislabel an invalid success body as a network outage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("not-json", { status: 200, headers: { "Content-Type": "text/plain" } })
    ));

    const result = await apiPost("/tasks", { user_goal: "test" });

    expect(result).toMatchObject({
      ok: false,
      error: { code: "INVALID_SERVICE_RESPONSE", recoverable: true },
    });
  });

  it("reports the EventSource connection lifecycle", () => {
    const states: string[] = [];
    let source: FakeEventSource | undefined;

    class FakeEventSource {
      static CLOSED = 2;
      readyState = 0;
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;

      constructor(_url: string) {
        source = this;
      }

      close() {
        this.readyState = FakeEventSource.CLOSED;
      }
    }

    vi.stubGlobal("EventSource", FakeEventSource);
    const unsubscribe = subscribeEvents("run-1", vi.fn(), (state) => states.push(state));

    source?.onopen?.();
    source?.onerror?.();
    unsubscribe();

    expect(states).toEqual(["connecting", "open", "reconnecting", "closed"]);
  });
});
