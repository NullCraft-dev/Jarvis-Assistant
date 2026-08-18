"""WorkspaceApplicationService — Workspace 注册、列表、撤销的 Application Service。

Workspace 选择是用户主动配置行为，不经过 Agent ToolGateway。
Workspace 注册结果写入 AuditLog。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from jarvis_worker.shared.errors.application import AppError
from jarvis_worker.runtime.workspaces.workspace_picker import WorkspacePickerPort
from jarvis_worker.shared.domain.models import (
    AuditLog,
    Workspace,
    WorkspaceSource,
    WorkspaceStatus,
    new_id,
    utcnow,
)
from jarvis_worker.database.unit_of_work import PostgresUnitOfWork

logger = logging.getLogger(__name__)

# ── 路径安全常量 ──

# 只禁止这些过宽根目录本身；其下明确的项目目录仍可注册。
_EXACT_FORBIDDEN: tuple[str, ...] = (
    "/",
    "/Users",
    "/private",
    "/var",
    "/tmp",
    "/Volumes",
)

# 绝对禁止注册的路径前缀（使用 os.path.commonpath 判断 containment）
# 禁止注册这些目录及其所有后代
_FORBIDDEN_TREES: tuple[str, ...] = (
    "/System",
    "/Library",
    "/Applications",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/dev",
    "/private/etc",
    "/private/var/db",
    "/private/var/root",
    "/private/var/log",
    "/private/var/run",
    "/private/var/audit",
)

# 敏感目录——在这些路径中或其任何子目录中拒绝注册
_SENSITIVE_ROOTS: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/Library/Keychains",
)

def _canonical(path: str) -> str:
    """返回 realpath 后的规范化绝对路径。"""
    return str(Path(path).expanduser().resolve(strict=False))


def _is_within(candidate: str, parent: str) -> bool:
    """判断 candidate 是否在 parent 路径内部（含等于）。"""
    try:
        return os.path.commonpath((candidate, parent)) == parent
    except ValueError:
        return False


def _is_sensitive(path: str) -> bool:
    """检查路径是否位于敏感目录中。"""
    for raw in _SENSITIVE_ROOTS:
        sensitive = _canonical(raw)
        if _is_within(path, sensitive):
            return True
    return False


def validate_path_for_registration(raw_path: str) -> str:
    """校验路径安全并返回 canonical_path。

    拒绝规则：
    1. 空路径
    2. 文件系统或其他过宽的顶层目录
    3. 用户 Home 根目录
    4. 能够覆盖用户 Home 的过宽父目录（如 /Users）
    5. 受保护系统目录及其后代
    6. 敏感目录及其后代（~/.ssh 等）
    7. /Volumes 本身（但其下明确子目录允许）
    8. 路径不存在或不是目录

    Raises:
        AppError: 路径不安全或无效。
    """
    trimmed = raw_path.strip()
    if not trimmed:
        raise AppError(
            code="VALIDATION_ERROR",
            message="路径不能为空",
            category="validation",
        )

    canonical = _canonical(trimmed)
    user_home = _canonical("~")

    # 1. 拒绝文件系统及其他过宽的顶层目录
    exact_forbidden = {_canonical(path) for path in _EXACT_FORBIDDEN}
    if canonical in exact_forbidden:
        raise AppError(
            code="WORKSPACE_PATH_FORBIDDEN",
            message="不允许使用过宽的系统根目录作为工作区",
            category="permission",
        )

    # 2. 拒绝用户 Home 根目录
    if canonical == user_home:
        raise AppError(
            code="WORKSPACE_PATH_FORBIDDEN",
            message="不允许直接使用用户主目录作为工作区",
            category="permission",
        )

    # 3. 拒绝能够覆盖用户 Home 的过宽父目录
    #    例如: /Users 包含了 ~/Library, ~/.ssh, ~/.aws 等
    if _is_within(user_home, canonical):
        raise AppError(
            code="WORKSPACE_PATH_FORBIDDEN",
            message="不允许使用能够覆盖用户主目录的过宽路径作为工作区",
            category="permission",
        )

    # 4. 拒绝受保护系统目录及其后代（使用 commonpath 判断，不依赖字符串前缀）
    for forbidden in _FORBIDDEN_TREES:
        if _is_within(canonical, _canonical(forbidden)):
            raise AppError(
                code="WORKSPACE_PATH_FORBIDDEN",
                message="不允许使用系统目录作为工作区",
                category="permission",
            )

    # 5. 拒绝敏感目录及其后代
    if _is_sensitive(canonical):
        raise AppError(
            code="WORKSPACE_PATH_FORBIDDEN",
            message="不允许使用敏感目录作为工作区",
            category="permission",
        )

    # 6. 路径必须存在且为目录
    if not os.path.isdir(canonical):
        raise AppError(
            code="WORKSPACE_NOT_FOUND",
            message="所选路径不存在或不是目录",
            category="validation",
        )

    return canonical


def verify_path_still_valid(stored_canonical: str) -> str:
    """在 Task 创建时重新校验已注册路径。

    重新解析 realpath，与保存的 canonical_path 比对。
    如果目录被删除、替换或改为指向其他位置的符号链接，fail closed。

    Returns:
        当前 realpath（必须等于 stored_canonical）。

    Raises:
        AppError: WORKSPACE_NOT_FOUND 或 WORKSPACE_PATH_FORBIDDEN。
    """
    current_real = _canonical(stored_canonical)
    if current_real != stored_canonical:
        raise AppError(
            code="WORKSPACE_PATH_FORBIDDEN",
            message="工作区路径已被修改或替换",
            category="permission",
        )
    # 重新应用完整安全策略，确保旧版本写入的危险 Workspace 也会 fail closed。
    return validate_path_for_registration(stored_canonical)


@dataclass
class PickWorkspaceResult:
    workspace: Workspace | None
    cancelled: bool = False


class WorkspaceApplicationService:
    """Workspace Application Service。"""

    def __init__(
        self,
        uow_factory,
        picker: WorkspacePickerPort | None = None,
    ):
        self._uow_factory = uow_factory
        self._picker = picker

    # ── 选择器 ──

    async def pick_workspace(self) -> PickWorkspaceResult:
        """通过系统目录选择器让用户选择一个目录并注册为 Workspace。"""
        if self._picker is None:
            return PickWorkspaceResult(workspace=None, cancelled=False)

        picker_result = await self._picker.pick_directory()

        if picker_result.cancelled:
            return PickWorkspaceResult(workspace=None, cancelled=True)

        if picker_result.error_code:
            raise AppError(
                code=picker_result.error_code,
                message=picker_result.error_message or "目录选择器错误",
                category="tool",
            )

        if not picker_result.selected_path:
            raise AppError(
                code="WORKSPACE_PICK_FAILED",
                message="未选择任何目录",
                category="tool",
            )

        # 安全校验
        canonical = validate_path_for_registration(picker_result.selected_path)
        # 使用目录名作为 Workspace 名称
        name = os.path.basename(canonical) or canonical

        workspace = await self._register(
            canonical,
            name,
            source=WorkspaceSource.USER_PICKER,
            root_path=picker_result.selected_path,
        )
        return PickWorkspaceResult(workspace=workspace, cancelled=False)

    # ── 注册 ──

    async def _register(
        self,
        canonical_path: str,
        name: str,
        source: WorkspaceSource = WorkspaceSource.USER_PICKER,
        root_path: str | None = None,
    ) -> Workspace:
        """注册 Workspace。

        使用 PostgreSQL INSERT ... ON CONFLICT 保证并发安全。
        同一 canonical_path 并发注册只产生一条记录。
        已 active → 幂等返回（不写 AuditLog）。
        已 revoked → 重新激活（写 reactivated AuditLog）。
        """
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                try:
                    now = utcnow()
                    workspace = Workspace(
                        id=new_id(),
                        name=name,
                        root_path=root_path or canonical_path,
                        canonical_path=canonical_path,
                        status=WorkspaceStatus.ACTIVE,
                        source=source,
                        created_at=now,
                        updated_at=now,
                    )
                    inserted = await tx.workspaces.insert_if_absent(workspace)
                    if inserted:
                        await self._write_audit(tx, workspace, "workspace.registered")
                        await tx.commit()
                        logger.info(
                            "Workspace 已注册: id=%s canonical=%s source=%s",
                            workspace.id, canonical_path, source.value,
                        )
                        return workspace

                    existing = await tx.workspaces.get_by_canonical_path_for_update(canonical_path)
                    if existing is None:
                        raise AppError(
                            code="WORKSPACE_REGISTRY_CONFLICT",
                            message="工作区注册发生并发冲突，请重试",
                            category="storage",
                            recoverable=True,
                        )

                    was_revoked = existing.status == WorkspaceStatus.REVOKED
                    became_configured = (
                        source == WorkspaceSource.CONFIGURED
                        and existing.source != WorkspaceSource.CONFIGURED
                    )
                    if existing.status == WorkspaceStatus.REVOKED:
                        existing.status = WorkspaceStatus.ACTIVE
                        existing.revoked_at = None
                        existing.updated_at = utcnow()
                        existing.name = name
                        existing.root_path = root_path or canonical_path
                    if became_configured:
                        # configured 是更强的管理来源，防止环境配置路径仍可被 Web revoke。
                        existing.source = WorkspaceSource.CONFIGURED
                        existing.root_path = root_path or canonical_path
                        existing.updated_at = utcnow()

                    if was_revoked or became_configured:
                        await tx.workspaces.update(existing)
                        event_type = (
                            "workspace.reactivated"
                            if was_revoked
                            else "workspace.managed_by_config"
                        )
                        await self._write_audit(tx, existing, event_type)
                    await tx.commit()
                    return existing

                except AppError:
                    await tx.rollback()
                    raise
                except Exception as e:
                    logger.error("注册 Workspace 失败: %s", e)
                    await tx.rollback()
                    raise AppError(
                        code="WORKSPACE_PICK_FAILED",
                        message="注册工作区失败",
                        category="storage",
                    ) from e

    async def register_configured(self, raw_path: str) -> Workspace | None:
        """启动时幂等注册配置中的允许路径（source=configured）。

        不会因为配置中路径无效而阻止启动；返回 None 表示跳过。
        """
        try:
            canonical = validate_path_for_registration(raw_path)
        except AppError as e:
            logger.warning("配置工作区跳过: path=%s reason=%s", raw_path, e.code)
            return None

        return await self._register(
            canonical, name=os.path.basename(canonical) or canonical,
            source=WorkspaceSource.CONFIGURED,
            root_path=raw_path,
        )

    async def register_all_configured(self, raw_paths: list[str]) -> list[Workspace]:
        """启动时批量注册所有配置路径。"""
        results: list[Workspace] = []
        for raw in raw_paths:
            ws = await self.register_configured(raw)
            if ws is not None:
                results.append(ws)
        return results

    # ── 查询 ──

    async def list_workspaces(self, include_revoked: bool = False) -> list[Workspace]:
        """列出 Workspace。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            if include_revoked:
                return await uow.workspaces.list_all()
            return await uow.workspaces.list_active()

    async def get_workspace(self, workspace_id: UUID) -> Workspace | None:
        """获取单个 Workspace。"""
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            return await uow.workspaces.get(workspace_id)

    # ── 撤销 ──

    async def revoke_workspace(self, workspace_id: UUID) -> Workspace:
        """撤销 Workspace（不物理删除）。

        source=configured 的 Workspace 不允许通过 Web 撤销。
        source=user_picker 的 Workspace 可以撤销。
        """
        session_factory = self._uow_factory()
        async with session_factory() as session:
            uow = PostgresUnitOfWork(session)
            async with uow.transaction() as tx:
                try:
                    workspace = await tx.workspaces.get_for_update(workspace_id)
                    if workspace is None:
                        raise AppError(
                            code="WORKSPACE_NOT_FOUND",
                            message=f"工作区不存在: {workspace_id}",
                            category="not_found",
                        )

                    # configured workspace 不允许通过 Web 撤销
                    if workspace.source == WorkspaceSource.CONFIGURED:
                        raise AppError(
                            code="WORKSPACE_MANAGED_BY_CONFIG",
                            message="该工作区由服务端配置管理，无法通过 Web 撤销",
                            category="permission",
                        )

                    if workspace.status == WorkspaceStatus.REVOKED:
                        # 已撤销，幂等返回
                        return workspace

                    workspace.status = WorkspaceStatus.REVOKED
                    workspace.revoked_at = utcnow()
                    workspace.updated_at = utcnow()
                    await tx.workspaces.update(workspace)
                    await self._write_audit(tx, workspace, "workspace.revoked")
                    await tx.commit()

                    logger.info(
                        "Workspace 已撤销: id=%s canonical=%s",
                        workspace_id, workspace.canonical_path,
                    )
                    return workspace

                except AppError:
                    await tx.rollback()
                    raise
                except Exception as e:
                    logger.error("撤销 Workspace 失败: %s", e)
                    await tx.rollback()
                    raise AppError(
                        code="WORKSPACE_PICK_FAILED",
                        message="撤销工作区失败",
                        category="storage",
                    ) from e

    # ── 创建 Task 前校验（在 Task 事务内调用）─

    async def validate_for_task_within_tx(
        self,
        tx: PostgresUnitOfWork,
        workspace_id: UUID,
    ) -> str:
        """在同一 UnitOfWork 事务内校验 workspace_id 并返回 canonical_path 快照。

        必须在 Task 创建事务内调用，避免"校验后撤销"竞态。

        Returns:
            当前 valid 的 canonical_path（用作 workspace_path 快照）。

        Raises:
            AppError: WORKSPACE_NOT_FOUND / WORKSPACE_REVOKED / WORKSPACE_PATH_FORBIDDEN。
        """
        workspace = await tx.workspaces.get_for_update(workspace_id)
        if workspace is None:
            raise AppError(
                code="WORKSPACE_NOT_FOUND",
                message=f"工作区不存在: {workspace_id}",
                category="not_found",
            )
        if workspace.status == WorkspaceStatus.REVOKED:
            raise AppError(
                code="WORKSPACE_REVOKED",
                message="该工作区已被撤销，无法用于新任务",
                category="permission",
            )

        # 重新验证路径在文件系统上仍然有效（防御 symlink 替换等）
        return verify_path_still_valid(workspace.canonical_path)

    # ── 审计日志 ──

    async def _write_audit(
        self,
        uow: PostgresUnitOfWork,
        workspace: Workspace,
        event_type: str,
    ) -> None:
        """写入 Workspace 相关审计日志。"""
        audit = AuditLog(
            id=new_id(),
            event_type=event_type,
            actor="user",
            action_summary=f"{event_type}: {workspace.name}",
            details={
                "workspace_id": str(workspace.id),
                "workspace_name": workspace.name,
                "canonical_path": workspace.canonical_path,
                "source": workspace.source.value,
            },
        )
        await uow.audits.create(audit)
