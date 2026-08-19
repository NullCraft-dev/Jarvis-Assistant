# 贡献指南

感谢你愿意帮助改进 Jarvis Assistant。项目欢迎缺陷报告、功能建议、文档修正和代码贡献。

## 开始之前

- 一般问题与缺陷请使用 GitHub Issues。
- 安全漏洞不要提交公开 Issue，请按 [SECURITY.md](SECURITY.md) 私下报告。
- 较大的功能或架构调整，请先创建 Issue 说明目标、使用场景和边界，避免重复工作。
- 请勿在 Issue、日志、截图、测试数据或提交中包含 API key、访问令牌、个人数据或真实用户内容。

## 本地开发

项目当前以 Web Agent 控制台为主入口，完整运行时由 Vue Web、Go Gateway、Redis、Python Worker 和 PostgreSQL 组成。

```bash
scripts/dev.sh setup
cp apps/agent-worker/.env.example apps/agent-worker/.env
scripts/dev.sh doctor
scripts/dev.sh start
```

请只在本地 `.env` 中填写自己的服务凭据。不要提交 `.env`、数据库、日志、运行产物或下载后的评测语料。

## 架构约束

提交代码时请保持以下边界：

- UI 通过类型化 API 和 Runtime Event 使用后端能力，不直接访问数据库、文件系统、Shell 或模型供应商。
- Go Gateway 负责入口契约、调度和事件扇出，不执行 Agent loop 或工具。
- Python AgentRunner 不直接访问本地或外部能力，所有工具调用必须经过 ToolGateway、PermissionManager、Storage 和 AuditLog。
- Redis 只承载运行时通信，不作为 Task、Run、Permission 或 AuditLog 的业务真源。
- 中高风险动作必须经过用户确认，高风险动作不得永久自动批准。

完整设计与契约见 [docs/README.md](docs/README.md)。

## 提交与验证

1. 从最新 `main` 创建主题分支。
2. 保持改动单一、可审查，并同步更新相关测试和文档。
3. 运行与改动风险匹配的检查；完整确定性门禁为：

   ```bash
   scripts/release-gate.sh ci
   ```

4. 提交 Pull Request，说明改动目的、影响范围、验证方式和已知限制。

不要通过降低权限、安全或测试门槛来让检查通过。若环境限制导致某项验证无法执行，请在 Pull Request 中明确说明。

## Pull Request 审查

维护者会重点检查：

- 分层和契约是否保持一致；
- 权限、审计、恢复和敏感信息处理是否完整；
- 新行为是否有机制级测试；
- 用户可见状态和错误是否可理解；
- 文档是否与代码同步。

提交贡献即表示你有权提交这些内容，并同意项目按仓库所列许可证分发你的贡献。
