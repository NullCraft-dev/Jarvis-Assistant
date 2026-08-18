import { defineStore } from "pinia";
import { ref } from "vue";
import type { CreateKnowledgeDocumentInput, KnowledgeDocumentDTO, KnowledgeVaultDTO } from "@jarvis/shared";
import * as api from "@/api/client";

export const useKnowledgeStore = defineStore("knowledge", () => {
  const vaults = ref<KnowledgeVaultDTO[]>([]);
  const documents = ref<KnowledgeDocumentDTO[]>([]);
  const suggestedPath = ref("");
  const loading = ref(false);
  const saving = ref(false);
  const error = ref<string | null>(null);

  async function load() {
    loading.value = true; error.value = null;
    try {
      const result = await api.listKnowledgeVaults();
      if (!result.ok) { error.value = result.error.message; return; }
      vaults.value = result.data.vaults; suggestedPath.value = result.data.suggested_path;
      if (vaults.value[0]) {
        const docs = await api.listKnowledgeDocuments(vaults.value[0].id);
        if (docs.ok) documents.value = docs.data.documents; else error.value = docs.error.message;
      } else documents.value = [];
    } catch { error.value = "知识库服务不可用"; }
    finally { loading.value = false; }
  }

  async function connect() {
    saving.value = true; error.value = null;
    try {
      const result = await api.connectKnowledgeVault({ path: suggestedPath.value });
      if (!result.ok) { error.value = result.error.message; return false; }
      await load(); return true;
    } catch { error.value = "连接知识库失败"; return false; }
    finally { saving.value = false; }
  }

  async function create(input: CreateKnowledgeDocumentInput) {
    const vault = vaults.value[0]; if (!vault) return false;
    saving.value = true; error.value = null;
    try {
      const result = await api.createKnowledgeDocument(vault.id, input);
      if (!result.ok) { error.value = result.error.message; return false; }
      documents.value.unshift(result.data.document); return true;
    } catch { error.value = "保存 Markdown 文档失败"; return false; }
    finally { saving.value = false; }
  }
  return { vaults, documents, suggestedPath, loading, saving, error, load, connect, create };
});
