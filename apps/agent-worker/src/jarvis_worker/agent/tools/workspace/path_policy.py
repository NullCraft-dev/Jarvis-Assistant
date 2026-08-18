"""Workspace capability tools 的共享路径安全策略。

本模块只拥有路径规范化、workspace 边界和安全目录 FD 遍历；具体工具行为由各工具模块拥有。
"""

from __future__ import annotations

import ctypes
import os
import stat
import sys

# 默认排除的噪声目录
_EXCLUDED_DIRS: set[str] = {
    "node_modules",
    ".git",
    ".svn",
    ".hg",
    "dist",
    "build",
    "__pycache__",
    ".cache",
    ".next",
    ".nuxt",
    ".tox",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    ".DS_Store",
}

def _resolve_safe_target(workspace_root: str, path: str) -> str:
    """将 workspace_root 和相对 path 组合，解析 realpath 并校验边界。

    步骤：
    1. 对 workspace_root 做 realpath 归一化
    2. 将 path 拼接到 workspace_root 下
    3. 对拼接结果做 realpath 归一化
    4. 校验 commonpath(target, root) == root

    Returns:
        安全的 target 绝对路径

    Raises:
        ValueError: 路径不在 workspace_root 内（路径穿越、绝对路径逃逸、symlink 逃逸）
        OSError: 路径解析失败
    """
    real_root = os.path.realpath(workspace_root)

    # 拼接 path 到 root 下
    # 注意：os.path.join 会忽略前面的参数如果 path 以 / 开头
    # 这是设计意图：如果用户传入 path="/etc"，join 结果就是 "/etc"
    # 后续 commonpath 校验会捕获这种情况
    joined = os.path.join(real_root, path)

    try:
        real_target = os.path.realpath(joined)
    except OSError:
        raise

    # 边界校验：共同父目录必须是 real_root
    try:
        common = os.path.commonpath([real_target, real_root])
    except ValueError:
        raise ValueError(f"路径边界校验失败: target={real_target}, root={real_root}")

    if common != real_root:
        raise ValueError(
            f"路径超出 workspace 范围: target={real_target}, root={real_root}"
        )

    return real_target

def _is_excluded(name: str) -> bool:
    """判断目录/文件名是否应排除。"""
    return name in _EXCLUDED_DIRS or name.startswith(".")

class _DirectorySymlinkError(OSError):
    """目录路径组件中出现 symlink。"""


def _supports_safe_search_dir_fd() -> bool:
    """当前平台是否支持 search_files 依赖的安全目录 FD 遍历。"""
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    return (
        all(hasattr(os, flag) for flag in required_flags)
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and os.scandir in os.supports_fd
    )


def _supports_safe_file_info_dir_fd() -> bool:
    """当前平台是否支持 get_file_info 依赖的安全 dir-fd 能力。"""
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    return (
        all(hasattr(os, flag) for flag in required_flags)
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


def _normalize_workspace_path(path: str) -> tuple[str, tuple[str, ...]]:
    """规范化 workspace 相对路径，返回 POSIX 展示路径和安全 components。"""
    if "\x00" in path:
        raise ValueError("path 含 NUL")
    normalized = os.path.normpath(path)
    if normalized == ".":
        return ".", ()
    components = tuple(
        part for part in normalized.split(os.sep) if part not in ("", ".")
    )
    if any(part == ".." for part in components):
        raise ValueError("path 超出 workspace 范围")
    return "/".join(components), components


def _parse_workspace_leaf_path(path: object) -> tuple[str, tuple[str, ...], str]:
    """校验一个必须指向 workspace 内非根节点的相对路径。

    返回 ``(display_path, parent_components, leaf_name)``。本函数只处理
    路径形状，不访问文件系统；调用方仍必须通过 dir-fd 打开父目录并对叶节点
    使用 ``follow_symlinks=False``。
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path 是必须参数")

    raw_path = path.strip()
    if os.path.isabs(raw_path):
        raise ValueError("不允许使用绝对路径")
    if "\x00" in raw_path:
        raise ValueError("path 含 NUL")

    # 即使 normpath 可以把 ``a/../b`` 规整为 ``b``，写操作也不接受任何
    # 原始 ``..`` 组件，避免调用方误以为其路径穿越语义会被保留。
    if any(part == ".." for part in raw_path.split(os.sep)):
        raise ValueError("path 包含 '..' component")

    display_path, components = _normalize_workspace_path(raw_path)
    if not components:
        raise ValueError("不允许操作 workspace 根目录")
    return display_path, components[:-1], components[-1]


def _open_workspace_root_fd(workspace_root: str) -> int:
    """解析并以不跟随符号链接的方式打开可信 workspace 根目录。"""
    real_root = os.path.realpath(workspace_root)
    return os.open(
        real_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )


def _supports_safe_mutation_dir_fd(*operations: object) -> bool:
    """判断当前平台是否支持写工具需要的 dir-fd 与防 symlink 能力。"""
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    return (
        all(hasattr(os, flag) for flag in required_flags)
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and all(operation in os.supports_dir_fd for operation in operations)
    )


def _rename_no_replace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """在两个可信目录 FD 间原子移动，且绝不覆盖既有目标。

    Python 标准库的 ``os.rename`` 在多数 POSIX 系统上会覆盖目标，不能用于
    本项目的 Workspace 写入契约。因此只使用平台提供的 no-replace 原语；没有
    该原语时调用方必须 fail closed，不能退回到“先检查、再 rename”。
    """
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)

    if sys.platform == "darwin":
        try:
            rename = libc.renameatx_np
        except AttributeError as exc:
            raise NotImplementedError("renameatx_np unavailable") from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        # macOS <sys/rename.h>: RENAME_EXCL
        result = rename(
            source_parent_fd,
            source_bytes,
            destination_parent_fd,
            destination_bytes,
            0x00000004,
        )
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as exc:
            raise NotImplementedError("renameat2 unavailable") from exc
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        # Linux <linux/fs.h>: RENAME_NOREPLACE
        result = rename(
            source_parent_fd,
            source_bytes,
            destination_parent_fd,
            destination_bytes,
            0x00000001,
        )
    else:
        raise NotImplementedError("atomic no-replace rename unsupported")

    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, "atomic no-replace rename failed")


def _open_workspace_directory_fd(root_fd: int, components: tuple[str, ...]) -> int:
    """从可信 root fd 逐级、无 symlink 地打开目录并返回新 FD。"""
    current_fd = os.dup(root_fd)
    try:
        for component in components:
            component_stat = os.stat(
                component,
                dir_fd=current_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(component_stat.st_mode):
                raise _DirectorySymlinkError("symlink directory component")
            if not stat.S_ISDIR(component_stat.st_mode):
                raise NotADirectoryError(component)
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise
