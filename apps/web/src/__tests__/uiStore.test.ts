import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";

import { useUiStore } from "@/stores/uiStore";

describe("uiStore responsive panels", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("keeps persistent desktop panel preferences", () => {
    const store = useUiStore();

    store.toggleSidebar();
    store.toggleInspector();

    expect(store.sidebarCollapsed).toBe(true);
    expect(store.inspectorVisible).toBe(false);
    expect(store.sidebarDrawerOpen).toBe(false);
    expect(store.inspectorDrawerOpen).toBe(false);
  });

  it("uses mutually exclusive overlay drawers in compact layout", () => {
    const store = useUiStore();
    store.setCompactLayout(true);

    store.toggleSidebar();
    expect(store.sidebarDrawerOpen).toBe(true);
    expect(store.inspectorDrawerOpen).toBe(false);

    store.toggleInspector();
    expect(store.sidebarDrawerOpen).toBe(false);
    expect(store.inspectorDrawerOpen).toBe(true);
  });

  it("opens the selected inspector tab without squeezing compact content", () => {
    const store = useUiStore();
    store.setCompactLayout(true);
    store.toggleSidebar();

    store.openInspector("tools");

    expect(store.inspectorTab).toBe("tools");
    expect(store.inspectorDrawerOpen).toBe(true);
    expect(store.sidebarDrawerOpen).toBe(false);
    expect(store.inspectorVisible).toBe(true);
  });

  it("closes transient drawers when returning to desktop", () => {
    const store = useUiStore();
    store.setCompactLayout(true);
    store.toggleInspector();

    store.setCompactLayout(false);

    expect(store.compactLayout).toBe(false);
    expect(store.sidebarDrawerOpen).toBe(false);
    expect(store.inspectorDrawerOpen).toBe(false);
    expect(store.inspectorVisible).toBe(true);
  });
});
