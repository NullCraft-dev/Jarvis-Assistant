# Changelog

## 1.0.0 - 2026-08-18

首个公开开发预览版本。

### Included

- Vue Web Agent 控制台与类型化前后端契约。
- Go Gateway、Redis Runtime Bus、Python Agent/RAG Worker。
- PostgreSQL 持久化、权限审批、审计、Artifact 与本地工作区工具。
- PDF RAG、页码引用、多文档检索和知识库笔记。
- 任务刷新、暂停、恢复、取消及关键故障恢复链路。
- 首次启动检查、结构化运行诊断和确定性 CI 门禁。
- 公开前依赖安全基线：修复 npm、LangGraph、Checkpoint、SDK、Cryptography 和 pytest 已知漏洞告警。
- 日志初始化可与 pytest、Uvicorn 等宿主 handler 共存，并只释放自身创建的 handler。
- Python 开发环境与 CI 通过 `uv.lock` 冻结同步，同时保留标准 `pip install .` 部署兼容性。

### Validation

- 完整 P0 真实使用评测：36/36 passed。
- 官方结果校验：`all_p0_passed`。
- 发布候选判定：`release_candidate_eligible=true`。

### Known boundaries

- 首版以本地 Web 控制台和 single-agent 为主。
- 本地视觉解析与 reranker 属于可选运行时。
- 桌面封装、语音和插件市场尚未包含。
- 当前版本不承诺生产适用性、稳定性、兼容性、安全认证、SLA 或商业支持。
