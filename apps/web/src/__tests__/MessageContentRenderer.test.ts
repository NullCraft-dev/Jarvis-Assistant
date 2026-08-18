// @vitest-environment happy-dom

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import MessageContentRenderer from "@/features/command/components/MessageContentRenderer.vue";
import { renderAssistantContent } from "@/features/command/messageContent";

describe("MessageContentRenderer", () => {
  it("renders assistant markdown instead of leaking markdown markers", () => {
    const wrapper = mount(MessageContentRenderer, {
      props: {
        role: "assistant",
        content: "## 结论\n\n**重点**\n\n- 第一项\n- 第二项",
      },
    });

    expect(wrapper.find("h2").text()).toBe("结论");
    expect(wrapper.find("strong").text()).toBe("重点");
    expect(wrapper.findAll("li")).toHaveLength(2);
    expect(wrapper.text()).not.toContain("**");
  });

  it("keeps user input as literal plain text", () => {
    const wrapper = mount(MessageContentRenderer, {
      props: { role: "user", content: "**不要渲染**" },
    });

    expect(wrapper.text()).toBe("**不要渲染**");
    expect(wrapper.find("strong").exists()).toBe(false);
  });

  it("renders a whole JSON answer in a dedicated viewer", () => {
    const wrapper = mount(MessageContentRenderer, {
      props: { role: "assistant", content: '{"status":"ok","count":2}' },
    });

    expect(wrapper.text()).toContain("JSON");
    expect(wrapper.find("pre").text()).toContain('"status": "ok"');
  });

  it("renders inline and display LaTeX with KaTeX", () => {
    const wrapper = mount(MessageContentRenderer, {
      props: {
        role: "assistant",
        content: String.raw`折扣因子 \(\gamma \in [0,1]\)。

\[
V^\pi(s)=\mathbb{E}\left[\sum_{t=0}^{T}\gamma^t r_t\right]
\]`,
      },
    });

    expect(wrapper.findAll(".katex").length).toBeGreaterThanOrEqual(2);
    expect(wrapper.find(".katex-display").exists()).toBe(true);
    expect(wrapper.find(".katex-html").text()).toContain("γ");
  });

  it("supports dollar math and keeps malformed formulas visible", () => {
    const rendered = renderAssistantContent(
      String.raw`Euler: $e^{i\pi}+1=0$

Malformed: $\notARealCommand{$`,
    );

    expect(rendered.kind).toBe("markdown");
    if (rendered.kind === "markdown") {
      expect(rendered.html).toContain('class="katex"');
      expect(rendered.html).toContain("notARealCommand");
    }
  });

  it("defensively unwraps a leaked finish action", () => {
    const rendered = renderAssistantContent(
      JSON.stringify({ action_type: "finish", final_message: "**真实回答**" }),
    );

    expect(rendered.kind).toBe("markdown");
    if (rendered.kind === "markdown") {
      expect(rendered.html).toContain("<strong>真实回答</strong>");
      expect(rendered.html).not.toContain("action_type");
    }
  });

  it("escapes raw html and rejects unsafe links", () => {
    const wrapper = mount(MessageContentRenderer, {
      props: {
        role: "assistant",
        content: '<script>alert(1)</script>\n\n[危险链接](javascript:alert(1))',
      },
    });

    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.html()).toContain("&lt;script&gt;");
    expect(wrapper.find("a").exists()).toBe(false);
  });

  it("renders trusted internal citation links in the same app tab", () => {
    const wrapper = mount(MessageContentRenderer, {
      props: {
        role: "assistant",
        content: "[引用 1](/knowledge/rag?document_id=11111111-1111-4111-8111-111111111111&chunk_id=22222222-2222-4222-8222-222222222222)",
      },
    });

    const link = wrapper.get("a");
    expect(link.attributes("href")).toContain("/knowledge/rag?document_id=");
    expect(link.attributes("target")).toBeUndefined();
  });

  it("does not turn bare filenames or dotted identifiers into external links", () => {
    const rendered = renderAssistantContent("worker.py、procedure.md、policy.md 与 cs.AI");

    expect(rendered.kind).toBe("markdown");
    if (rendered.kind === "markdown") {
      expect(rendered.html).not.toContain("<a ");
    }
  });

  it("still linkifies explicit protocol URLs", () => {
    const rendered = renderAssistantContent("参考 https://example.com/docs");

    expect(rendered.kind).toBe("markdown");
    if (rendered.kind === "markdown") {
      expect(rendered.html).toContain('href="https://example.com/docs"');
      expect(rendered.html).toContain('target="_blank"');
    }
  });

  it("keeps long prose, code, tables, quotes, and images inside the message boundary", () => {
    const longToken = "a".repeat(180);
    const wrapper = mount(MessageContentRenderer, {
      props: {
        role: "assistant",
        content: [
          `路径：/workspace/${longToken}`,
          "",
          `\`${longToken}\``,
          "",
          "```text",
          longToken,
          "```",
          "",
          "| 第一列 | 第二列 |",
          "| --- | --- |",
          `| ${longToken} | ${longToken} |`,
          "",
          `> ${longToken}`,
          "",
          "![图](https://example.com/image.png)",
        ].join("\n"),
      },
    });

    expect(wrapper.classes()).toContain("max-w-full");
    expect(wrapper.get("pre").element.textContent).toContain(longToken);
    expect(wrapper.get("table").element.tagName).toBe("TABLE");
    expect(wrapper.get("blockquote").element.tagName).toBe("BLOCKQUOTE");
    expect(wrapper.get("img").element.tagName).toBe("IMG");
  });
});
