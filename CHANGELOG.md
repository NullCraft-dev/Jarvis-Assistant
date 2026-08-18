# Changelog

## 0.1.0 - 2026-08-18

首个私有预览版本。

### Included

- Vue Web Agent 控制台与类型化前后端契约。
- Go Gateway、Redis Runtime Bus、Python Agent/RAG Worker。
- PostgreSQL 持久化、权限审批、审计、Artifact 与本地工作区工具。
- PDF RAG、页码引用、多文档检索和知识库笔记。
- 任务刷新、暂停、恢复、取消及关键故障恢复链路。
- 首次启动检查、结构化运行诊断和确定性 CI 门禁。

### Validation

- 完整 P0 真实使用评测：36/36 passed。
- 官方结果校验：`all_p0_passed`。
- 发布候选判定：`release_candidate_eligible=true`。

### Known boundaries

- 首版以本地 Web 控制台和 single-agent 为主。
- 本地视觉解析与 reranker 属于可选运行时。
- 桌面封装、语音和插件市场尚未包含。
