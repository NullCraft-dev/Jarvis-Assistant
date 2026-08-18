# Security Policy

## Supported version

当前私有预览仅支持最新的 `0.1.x` 版本。

## Reporting a vulnerability

请不要在普通 Issue、讨论区、提交信息或日志附件中公开安全漏洞、密钥、访问令牌、个人数据或可利用细节。

在 GitHub 仓库中优先使用 **Security → Report a vulnerability** 私下提交。报告应包含：

- 受影响版本或 commit；
- 最小复现步骤；
- 可能影响的权限、数据或工作区范围；
- 已知的缓解方式。

收到报告后会先确认影响范围，再决定修复、轮换凭据和发布安排。

## Secret handling

- 不提交 `.env`、API key、token、私钥、数据库备份或真实用户内容。
- 示例凭据必须是明确不可用的测试值。
- 怀疑凭据泄漏时，应立即在供应商侧撤销并轮换；仅从 Git 删除文件不足以使历史中的凭据失效。
