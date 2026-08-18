# Permissions

`permissions/` 负责权限策略、风险分级和授权规则。

当前 `PermissionManager` 位于 `agent/permissions/manager.py`。后续 risk level
判断、grant、approval scope、policy decision 都应继续放在这里。

权限策略不执行工具，也不直接持久化最终审计真相。
