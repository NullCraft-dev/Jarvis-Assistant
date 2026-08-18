// @vitest-environment happy-dom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import CommandComposer from "@/features/command/components/CommandComposer.vue";

describe("CommandComposer", () => {
  it("keeps the draft after emitting submit so the parent can clear it only on success", async () => {
    const wrapper = mount(CommandComposer, {
      props: { modelValue: "" },
    });
    const textarea = wrapper.get("textarea");
    await textarea.setValue("保留这条任务指令");
    await wrapper.get("button").trigger("click");

    expect(wrapper.emitted("submit")?.[0]).toEqual(["保留这条任务指令"]);
    expect((textarea.element as HTMLTextAreaElement).value).toBe("保留这条任务指令");
  });

  it("disables both input and submit action while a run is active", () => {
    const wrapper = mount(CommandComposer, {
      props: { modelValue: "下一条任务", disabled: true },
    });

    expect(wrapper.get("textarea").attributes("disabled")).toBeDefined();
    expect(wrapper.get("button").attributes("disabled")).toBeDefined();
  });
});
