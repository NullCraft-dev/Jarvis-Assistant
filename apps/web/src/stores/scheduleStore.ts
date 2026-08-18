import { defineStore } from "pinia";
import { ref } from "vue";
import type { CreateScheduledTaskInput, ScheduledTaskDTO } from "@jarvis/shared";
import * as api from "@/api/client";

export const useScheduleStore = defineStore("schedules", () => {
  const items=ref<ScheduledTaskDTO[]>([]), loading=ref(false), saving=ref(false), error=ref<string|null>(null);
  async function load(){loading.value=true;error.value=null;try{const r=await api.listScheduledTasks();if(r.ok)items.value=r.data.scheduled_tasks;else error.value=r.error.message}catch{error.value="定期任务服务不可用"}finally{loading.value=false}}
  async function create(input:CreateScheduledTaskInput){saving.value=true;error.value=null;try{const r=await api.createScheduledTask(input);if(!r.ok){error.value=r.error.message;return false}items.value.unshift(r.data.scheduled_task);return true}catch{error.value="创建定期任务失败";return false}finally{saving.value=false}}
  async function toggle(item:ScheduledTaskDTO){saving.value=true;try{const r=await api.updateScheduledTask(item.id,{expected_version:item.version,status:item.status==="active"?"paused":"active"});if(!r.ok){error.value=r.error.message;return}await load()}finally{saving.value=false}}
  async function trigger(item:ScheduledTaskDTO){saving.value=true;error.value=null;try{const r=await api.triggerScheduledTask(item.id);if(!r.ok){error.value=r.error.message;return false}await load();return true}catch{error.value="手动触发失败";return false}finally{saving.value=false}}
  return{items,loading,saving,error,load,create,toggle,trigger};
});
