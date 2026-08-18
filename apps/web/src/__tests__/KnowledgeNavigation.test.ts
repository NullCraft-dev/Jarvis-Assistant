// @vitest-environment happy-dom

import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { describe, expect, it } from "vitest";
import { defineComponent } from "vue";

import KnowledgeView from "@/views/KnowledgeView.vue";

const EmptyView = defineComponent({ template: "<div>内容</div>" });
const RouterRoot = defineComponent({ template: "<RouterView />" });

function createKnowledgeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [{
      path: "/knowledge",
      component: KnowledgeView,
      children: [
        { path: "", component: EmptyView },
        { path: "documents", component: EmptyView },
        { path: "rag", component: EmptyView },
        { path: "quality", component: EmptyView },
      ],
    }],
  });
}

describe("Knowledge navigation", () => {
  it("exposes overview, documents, RAG, and quality as separate routes", async () => {
    const router = createKnowledgeRouter();
    await router.push("/knowledge");
    await router.isReady();
    const wrapper = mount(RouterRoot, { global: { plugins: [router] } });

    const links = wrapper.findAll("nav a");
    expect(links.map((link) => link.text())).toEqual(["总览", "知识文档", "RAG 文档", "RAG 质量"]);
    expect(links.map((link) => link.attributes("href"))).toEqual([
      "/knowledge",
      "/knowledge/documents",
      "/knowledge/rag",
      "/knowledge/quality",
    ]);
    expect(links[0]!.classes()).toContain("border-blue-500");

    await router.push("/knowledge/rag");
    await flushPromises();
    expect(wrapper.findAll("nav a")[2]!.classes()).toContain("border-blue-500");
    expect(wrapper.findAll("nav a")[0]!.classes()).toContain("border-transparent");
  });
});
