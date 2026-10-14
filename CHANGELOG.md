# Changelog

## 1.0.1 - 2026-10-14

### Fixed

- 修复显式使用 DashScope/Qwen Embedding 时 Agent Worker 与 RAG Worker 无法启动的问题。
- 新增独立 DashScope Provider 与共享 factory；保留 OpenAI 默认值、1536 维索引和旧工厂入口。
- Provider 错误通过索引和检索链路保留安全错误码与 recoverable 状态，批大小有界。
- 索引目标正确记录 Provider；切换模型不自动迁移或重建已有知识库。

### Validation

- Python 全量测试 1649 passed；Web 测试 148 passed；类型、构建、Go test/vet 与 Ruff 检查通过。
- 显式 DashScope 配置的真实请求成功返回 1536 维向量。
- 未重新声明 v1.0.0 的 36/36 P0 真实使用结果为本补丁验收。

### Upgrade

- 使用 Qwen 时同时设置 provider、model、api_key_env 和 batch_size，见开发指南。
- 无新增 migration；已有向量保留，切换 Provider/模型后需显式重建相应文档索引。

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
