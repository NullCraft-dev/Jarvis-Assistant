# Security Policy

## Supported versions

安全修复优先提供给最新的 `1.x` 版本。较早版本可能不再获得补丁，请在报告中注明受影响版本或 commit。

## Reporting a vulnerability

请不要在普通 Issue、讨论区、提交信息或日志附件中公开安全漏洞、密钥、访问令牌、个人数据或可利用细节。

请使用 GitHub 仓库的 **Security → Report a vulnerability** 私下提交：

<https://github.com/NullCraft-dev/Jarvis-Assistant/security/advisories/new>

报告应包含：

- 受影响版本或 commit；
- 最小复现步骤；
- 可能影响的权限、数据或工作区范围；
- 已知的缓解方式。

维护者会尽力在 7 天内确认收到报告，但当前社区项目不承诺固定修复时限。确认问题后，会根据影响范围协调修复、凭据轮换、公告和版本发布。在修复可用前，请不要公开可利用细节。

## Scope

以下问题通常属于安全范围：

- 绕过 ToolGateway 或 PermissionManager 执行本地或外部动作；
- 未经授权访问其他工作区、任务、Artifact、记忆或凭据；
- 路径穿越、符号链接逃逸、命令注入或敏感日志泄漏；
- 高风险操作在未确认情况下执行，或被永久自动批准；
- 可导致持久数据破坏、权限提升或远程代码执行的问题。

普通功能缺陷、无法复现的模型回答质量问题，以及不涉及安全边界的本地配置错误，请使用普通 Issue。

## Secret handling

- 不提交 `.env`、API key、token、私钥、数据库备份或真实用户内容。
- 示例凭据必须是明确不可用的测试值。
- 怀疑凭据泄漏时，应立即在供应商侧撤销并轮换；仅从 Git 删除文件不足以使历史中的凭据失效。
