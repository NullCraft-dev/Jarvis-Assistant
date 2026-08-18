import { defineStore } from "pinia";
import { ref } from "vue";
import type { CreateMcpServerInput, McpServerDTO } from "@jarvis/shared";
import * as api from "@/api/client";

export const useMcpStore = defineStore("mcp", () => {
  const servers = ref<McpServerDTO[]>([]), loading = ref(false), saving = ref(false);
  const error = ref<string | null>(null), restartRequired = ref(false);
  const refreshCommandId = ref<string | null>(null);
  async function load() { loading.value=true;error.value=null;try{const r=await api.listMcpServers();if(r.ok)servers.value=r.data.servers;else error.value=r.error.message}catch{error.value="MCP 服务不可用"}finally{loading.value=false} }
  async function create(input:CreateMcpServerInput){saving.value=true;error.value=null;try{const r=await api.createMcpServer(input);if(!r.ok){error.value=r.error.message;return false}restartRequired.value=r.data.worker_restart_required;await load();return true}catch{error.value="MCP server 注册失败";return false}finally{saving.value=false}}
  async function toggle(item:McpServerDTO){saving.value=true;error.value=null;try{const r=await api.updateMcpServer(item.id,{enabled:!item.enabled,expected_version:item.version});if(!r.ok){error.value=r.error.message;return}restartRequired.value=r.data.worker_restart_required;await load()}finally{saving.value=false}}
  async function refresh(){saving.value=true;error.value=null;try{const r=await api.refreshMcpServers();if(!r.ok){error.value=r.error.message;return}refreshCommandId.value=r.data.command_id;restartRequired.value=r.data.worker_restart_required}catch{error.value="工具发现请求发送失败"}finally{saving.value=false}}
  async function connectLiterature(){saving.value=true;error.value=null;try{const r=await api.connectBuiltinLiteratureServer();if(!r.ok){error.value=r.error.message;return}restartRequired.value=r.data.worker_restart_required;await load()}catch{error.value="内置文献来源连接失败"}finally{saving.value=false}}
  return {servers,loading,saving,error,restartRequired,refreshCommandId,load,create,toggle,refresh,connectLiterature};
});
