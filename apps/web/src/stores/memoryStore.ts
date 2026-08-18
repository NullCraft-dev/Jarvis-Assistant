import { defineStore } from "pinia";
import { ref } from "vue";
import type {
  CreateMemoryInput, MemoryCandidateDTO, MemoryDTO, ResolveMemoryCandidateInput,
  UpdateMemoryCandidateInput, UpdateMemoryInput,
} from "@jarvis/shared";
import {
  approveMemoryCandidate, createMemory, deleteMemory, listMemories,
  listMemoryCandidates, rejectMemoryCandidate, updateMemory, updateMemoryCandidate,
} from "@/api/client";

export const useMemoryStore = defineStore("memory", () => {
  const memories = ref<MemoryDTO[]>([]);
  const candidates = ref<MemoryCandidateDTO[]>([]);
  const loading = ref(false);
  const saving = ref(false);
  const error = ref<string | null>(null);

  async function load() {
    loading.value = true; error.value = null;
    try {
      const [memoryResult, candidateResult] = await Promise.all([
        listMemories(), listMemoryCandidates("status=pending"),
      ]);
      if (!memoryResult.ok) { error.value = memoryResult.error.message; return; }
      if (!candidateResult.ok) { error.value = candidateResult.error.message; return; }
      memories.value = memoryResult.data.memories;
      candidates.value = candidateResult.data.candidates;
    } catch { error.value = "长期记忆服务不可用"; }
    finally { loading.value = false; }
  }

  async function create(input: CreateMemoryInput): Promise<boolean> {
    saving.value = true; error.value = null;
    try {
      const result = await createMemory(input);
      if (!result.ok) { error.value = result.error.message; return false; }
      memories.value.unshift(result.data.memory); return true;
    } catch { error.value = "创建记忆失败"; return false; }
    finally { saving.value = false; }
  }

  async function update(id: string, input: UpdateMemoryInput): Promise<boolean> {
    saving.value = true; error.value = null;
    try {
      const result = await updateMemory(id, input);
      if (!result.ok) { error.value = result.error.message; return false; }
      memories.value = memories.value.map((item) => item.id === id ? result.data.memory : item);
      return true;
    } catch { error.value = "更新记忆失败"; return false; }
    finally { saving.value = false; }
  }

  async function remove(id: string): Promise<boolean> {
    saving.value = true; error.value = null;
    try {
      const result = await deleteMemory(id);
      if (!result.ok) { error.value = result.error.message; return false; }
      memories.value = memories.value.filter((item) => item.id !== id); return true;
    } catch { error.value = "删除记忆失败"; return false; }
    finally { saving.value = false; }
  }

  async function updateCandidate(id: string, input: UpdateMemoryCandidateInput): Promise<boolean> {
    saving.value = true; error.value = null;
    try {
      const result = await updateMemoryCandidate(id, input);
      if (!result.ok) { error.value = result.error.message; return false; }
      candidates.value = candidates.value.map((item) => item.id === id ? result.data.candidate : item);
      return true;
    } catch { error.value = "更新记忆候选失败"; return false; }
    finally { saving.value = false; }
  }

  async function approveCandidate(id: string, input: ResolveMemoryCandidateInput): Promise<boolean> {
    saving.value = true; error.value = null;
    try {
      const result = await approveMemoryCandidate(id, input);
      if (!result.ok) { error.value = result.error.message; return false; }
      candidates.value = candidates.value.filter((item) => item.id !== id);
      memories.value = [result.data.memory, ...memories.value.filter((item) => item.id !== result.data.memory.id)];
      return true;
    } catch { error.value = "批准记忆候选失败"; return false; }
    finally { saving.value = false; }
  }

  async function rejectCandidate(id: string, input: ResolveMemoryCandidateInput): Promise<boolean> {
    saving.value = true; error.value = null;
    try {
      const result = await rejectMemoryCandidate(id, input);
      if (!result.ok) { error.value = result.error.message; return false; }
      candidates.value = candidates.value.filter((item) => item.id !== id);
      return true;
    } catch { error.value = "拒绝记忆候选失败"; return false; }
    finally { saving.value = false; }
  }

  return {
    memories, candidates, loading, saving, error, load, create, update, remove,
    updateCandidate, approveCandidate, rejectCandidate,
  };
});
