"""workspace.create_file capability executor。"""

from __future__ import annotations

import hashlib
import logging
import os
import stat

from jarvis_worker.agent.tool_gateway.contracts import ToolDeliverable, ToolRequest, ToolResult

log = logging.getLogger("jarvis_worker.tool.workspace")

# ============================================================
# workspace.create_file — L2 Scoped Write
# ============================================================

# 单文件内容最大字节数（1 MiB）
_MAX_CONTENT_BYTES = 1 * 1024 * 1024  # 1048576


def _sha256_hex(data: bytes) -> str:
    """计算 sha256 十六进制摘要（完整 64 hex）。"""
    return hashlib.sha256(data).hexdigest()


def _path_components(rel_path: str) -> tuple[list[str], str]:
    """把相对路径拆成父目录 components 和最终 filename。

    'a/b/c.txt' → (['a','b'], 'c.txt')
    'file.txt'  → ([], 'file.txt')
    """
    parts = [p for p in rel_path.split(os.sep) if p not in ("", ".")]
    if not parts:
        raise ValueError("empty path")
    return parts[:-1], parts[-1]


def _validate_path_components(components: list[str], filename: str) -> None:
    """拒绝空 filename、含 NUL、绝对路径或单独 '..' 的 component。"""
    if not filename:
        raise ValueError("filename 不能为空")
    if "\x00" in filename:
        raise ValueError("filename 含 NUL")
    for comp in components + [filename]:
        if "\x00" in comp:
            raise ValueError("path component 含 NUL")
        if comp == "..":
            raise ValueError("path 包含 '..' component")
        if os.path.isabs(comp):
            raise ValueError("path component 是绝对路径")


def _supports_safe_dir_fd() -> bool:
    """当前平台是否支持本工具依赖的安全 dir-fd 能力。"""
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    return (
        all(hasattr(os, flag) for flag in required_flags)
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


def _existing_target_result(parent_fd: int, filename: str) -> ToolResult:
    """在可信 parent fd 下分类已经存在的目标，不跟随符号链接。"""
    try:
        target_stat = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return ToolResult(
            ok=False,
            kind="empty",
            summary="文件已存在，不覆盖",
            error={
                "code": "FILE_ALREADY_EXISTS",
                "message": "目标已存在，workspace.create_file 不覆盖已有目标",
                "category": "validation",
                "recoverable": True,
            },
        )

    if stat.S_ISDIR(target_stat.st_mode):
        return ToolResult(
            ok=False,
            kind="empty",
            summary="目标路径是目录",
            error={
                "code": "PATH_IS_DIRECTORY",
                "message": "目标路径是一个目录，不能创建为文件",
                "category": "validation",
                "recoverable": True,
            },
        )
    if stat.S_ISLNK(target_stat.st_mode):
        return ToolResult(
            ok=False,
            kind="empty",
            summary="拒绝符号链接目标",
            error={
                "code": "WORKSPACE_ACCESS_DENIED",
                "message": "目标路径是符号链接，拒绝创建",
                "category": "permission",
                "recoverable": False,
            },
        )
    return ToolResult(
        ok=False,
        kind="empty",
        summary="文件已存在，不覆盖",
        error={
            "code": "FILE_ALREADY_EXISTS",
            "message": "文件已存在，workspace.create_file 不覆盖已有文件",
            "category": "validation",
            "recoverable": True,
        },
    )


def execute_workspace_create_file(request: ToolRequest) -> ToolResult:
    """执行 workspace.create_file — 在 Workspace 内安全创建新 UTF-8 文本文件。

    L2 Scoped Write：必须经过用户权限确认。

    安全策略（dir-fd 逐级遍历，无 TOCTOU）：
    - 从 Workspace root fd 开始，O_DIRECTORY|O_NOFOLLOW 逐级打开父目录
    - 任意父级是 symlink → fail closed
    - 最终文件通过可信 parent dir fd + O_CREAT|O_EXCL|O_NOFOLLOW 创建
    - memoryview 循环写入 + fsync 保证完整
    - 失败时通过 parent fd + filename 安全清理（校验 st_dev/st_ino 防目录替换）
    - 用户可见错误不含绝对路径、OSError 原文或 content
    """

    # ── 0. 平台检查 ──
    if not _supports_safe_dir_fd():
        return ToolResult(
            ok=False, kind="empty", summary="当前平台不支持安全文件创建",
            error={"code": "UNSUPPORTED_PLATFORM", "message": "当前平台不支持 dir_fd 安全文件创建",
                   "category": "tool", "recoverable": False},
        )

    args = request.arguments if request.arguments else {}

    # ── 1. workspace_root ──
    workspace_root: str = args.get("workspace_root", "")
    if not workspace_root or not isinstance(workspace_root, str):
        return ToolResult(ok=False, kind="empty", summary="workspace_root 缺失",
                          error={"code": "WORKSPACE_ROOT_REQUIRED", "message": "workspace_root 是必须参数",
                                 "category": "permission", "recoverable": False})

    # ── 2. path 校验 ──
    path_raw = args.get("path", "")
    if not path_raw or not isinstance(path_raw, str) or not path_raw.strip():
        return ToolResult(ok=False, kind="empty", summary="path 参数缺失",
                          error={"code": "TOOL_ARGUMENTS_INVALID", "message": "path 是必须参数",
                                 "category": "validation", "recoverable": True})
    path = path_raw.strip()

    if os.path.isabs(path):
        return ToolResult(ok=False, kind="empty", summary="绝对路径不允许",
                          error={"code": "WORKSPACE_ACCESS_DENIED", "message": "不允许使用绝对路径",
                                 "category": "permission", "recoverable": True})

    # 拆解 components
    try:
        components, filename = _path_components(path)
        _validate_path_components(components, filename)
    except ValueError as e:
        return ToolResult(ok=False, kind="empty", summary=f"路径无效: {e}",
                          error={"code": "TOOL_ARGUMENTS_INVALID", "message": str(e),
                                 "category": "validation", "recoverable": True})

    # reject ../foo
    for c in components:
        if c == "..":
            return ToolResult(ok=False, kind="empty", summary="路径穿越不允许",
                              error={"code": "WORKSPACE_ACCESS_DENIED",
                                     "message": "不允许使用路径穿越访问 workspace 外",
                                     "category": "permission", "recoverable": False})

    # ── 3. content ──
    content_raw = args.get("content")
    if content_raw is None or not isinstance(content_raw, str):
        return ToolResult(ok=False, kind="empty", summary="content 参数缺失",
                          error={"code": "TOOL_ARGUMENTS_INVALID", "message": "content 是必须参数",
                                 "category": "validation", "recoverable": True})

    content_bytes = content_raw.encode("utf-8")
    if len(content_bytes) > _MAX_CONTENT_BYTES:
        return ToolResult(ok=False, kind="empty",
                          summary=f"内容超过 {_MAX_CONTENT_BYTES} 字节上限",
                          error={"code": "FILE_TOO_LARGE",
                                 "message": f"文件内容超过 {_MAX_CONTENT_BYTES} 字节（1 MiB）上限",
                                 "category": "validation", "recoverable": True})

    # ── 4. Workspace root 校验 + 打开 root fd ──
    try:
        real_root = os.path.realpath(workspace_root)
    except OSError:
        return ToolResult(ok=False, kind="empty", summary="workspace 根目录解析失败",
                          error={"code": "WORKSPACE_ACCESS_DENIED",
                                 "message": "workspace 根目录无法解析", "category": "permission", "recoverable": False})

    try:
        root_fd = os.open(real_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except (OSError, NotImplementedError):
        return ToolResult(ok=False, kind="empty", summary="无法打开 workspace 根目录",
                          error={"code": "WORKSPACE_ACCESS_DENIED",
                                 "message": "无法安全打开 workspace 根目录", "category": "permission", "recoverable": False})

    # 逐级打开父目录
    parent_fd = root_fd
    fds_to_close = [root_fd]
    created_fd: int | None = None

    try:
        for comp in components:
            try:
                component_stat = os.stat(
                    comp,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(component_stat.st_mode):
                    return ToolResult(
                        ok=False,
                        kind="empty",
                        summary="拒绝符号链接路径组件",
                        error={
                            "code": "WORKSPACE_ACCESS_DENIED",
                            "message": "路径中包含符号链接，拒绝创建",
                            "category": "permission",
                            "recoverable": False,
                        },
                    )
            except FileNotFoundError:
                return ToolResult(ok=False, kind="empty", summary="父目录不存在",
                                  error={"code": "PARENT_DIR_NOT_FOUND",
                                         "message": "父目录不存在，workspace.create_file 不会自动创建目录",
                                         "category": "validation", "recoverable": True})
            except OSError:
                return ToolResult(ok=False, kind="empty", summary="无法访问路径组件",
                                  error={"code": "WORKSPACE_ACCESS_DENIED",
                                         "message": "无法安全访问路径组件",
                                         "category": "permission", "recoverable": False})
            try:
                next_fd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
            except FileNotFoundError:
                return ToolResult(ok=False, kind="empty", summary="父目录不存在",
                                  error={"code": "PARENT_DIR_NOT_FOUND",
                                         "message": "父目录不存在，workspace.create_file 不会自动创建目录",
                                         "category": "validation", "recoverable": True})
            except NotADirectoryError:
                return ToolResult(ok=False, kind="empty", summary="路径中包含非目录组件",
                                  error={"code": "PATH_IS_DIRECTORY",
                                         "message": "路径中包含非目录组件", "category": "validation", "recoverable": True})
            except OSError:
                return ToolResult(ok=False, kind="empty", summary="无法访问路径组件",
                                  error={"code": "WORKSPACE_ACCESS_DENIED",
                                         "message": "无法安全访问路径组件（可能包含符号链接）",
                                         "category": "permission", "recoverable": False})
            fds_to_close.append(next_fd)
            parent_fd = next_fd

        # ── 5. 安全创建文件 ──
        try:
            created_fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o644,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            return _existing_target_result(parent_fd, filename)
        except NotADirectoryError:
            return ToolResult(ok=False, kind="empty", summary="文件名与已有目录冲突",
                              error={"code": "PATH_IS_DIRECTORY",
                                     "message": "目标路径是一个目录", "category": "validation", "recoverable": True})
        except PermissionError:
            return ToolResult(ok=False, kind="empty", summary="权限不足",
                              error={"code": "PERMISSION_DENIED",
                                     "message": "没有权限在目标位置创建文件",
                                     "category": "permission", "recoverable": True})
        except OSError:
            return ToolResult(ok=False, kind="empty", summary="创建文件失败",
                              error={"code": "CREATE_FILE_FAILED",
                                     "message": "无法创建文件（目标可能是符号链接）",
                                     "category": "tool", "recoverable": True})
        # 保存创建时文件 stat（用于清理前校验）
        created_stat = os.fstat(created_fd)

        # ── 6. 循环写入 + fsync ──
        try:
            remaining = memoryview(content_bytes)
            while remaining:
                written = os.write(created_fd, remaining)
                if written <= 0:
                    raise OSError(f"os.write 返回 {written}")
                remaining = remaining[written:]
            os.fsync(created_fd)
        except OSError:
            # 清理：只删除自己创建的文件（校验 dev/ino）
            _safe_cleanup(parent_fd, filename, created_stat)
            return ToolResult(ok=False, kind="empty", summary="写入文件失败",
                              error={"code": "CREATE_FILE_FAILED",
                                     "message": "写入文件时发生系统错误，已清理不完整文件",
                                     "category": "tool", "recoverable": True})

        # ── 7. 结果 ──
        sha256_hex = _sha256_hex(content_bytes)
        size_bytes = len(content_bytes)
        rel_path = os.sep.join(components + [filename]) if components else filename

        log.info("workspace.create_file: path=%s size=%d sha256=%s", rel_path, size_bytes, sha256_hex)

        mime_type = _text_mime_type(rel_path)
        return ToolResult(
            ok=True, kind="file",
            summary=f"Created file: {rel_path} ({size_bytes} bytes)",
            data={"created": True, "path": rel_path, "size_bytes": size_bytes, "sha256": sha256_hex},
            deliverables=[ToolDeliverable(
                kind="file",
                title=rel_path,
                path=rel_path,
                size_bytes=size_bytes,
                mime_type=mime_type,
                content_hash=sha256_hex,
            )],
        )

    finally:
        # created fd 在成功和异常路径都由同一 finally 关闭；目录 fd 再逆序关闭。
        if created_fd is not None:
            try:
                os.close(created_fd)
            except OSError:
                pass
        for fd in reversed(fds_to_close):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _text_mime_type(path: str) -> str:
    """为受控 UTF-8 文本工具返回稳定、跨平台的 MIME。"""
    lowered = path.lower()
    if lowered.endswith((".md", ".markdown")):
        return "text/markdown; charset=utf-8"
    if lowered.endswith(".json"):
        return "application/json; charset=utf-8"
    if lowered.endswith((".diff", ".patch")):
        return "text/x-diff; charset=utf-8"
    return "text/plain; charset=utf-8"


def _safe_cleanup(parent_fd: int, filename: str, created_stat: os.stat_result) -> None:
    """通过 parent fd + filename 安全清理文件。

    清理前校验目录项 st_dev/st_ino 与创建时一致；
    如果目录项已被替换，不得 unlink 别人的文件。
    """
    try:
        current_stat = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if current_stat.st_dev != created_stat.st_dev or current_stat.st_ino != created_stat.st_ino:
            log.warning("create_file 清理取消: 目标已被替换 (dev=%d->%d ino=%d->%d)",
                        created_stat.st_dev, current_stat.st_dev,
                        created_stat.st_ino, current_stat.st_ino)
            return
        os.unlink(filename, dir_fd=parent_fd)
    except FileNotFoundError:
        pass  # 已经不存在
    except OSError as e:
        log.warning("create_file 清理失败: errno=%d type=%s", getattr(e, "errno", 0), type(e).__name__)
