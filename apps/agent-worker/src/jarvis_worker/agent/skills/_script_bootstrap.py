"""受控 Skill Python 脚本的最小启动器。

这不是面向不可信代码的 OS 沙箱。Skill 包必须先由 SkillLoader 校验并固定哈希；
本启动器额外阻断 Python 审计事件中的网络与子进程入口，防止确定性脚本意外扩大能力。
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

_BLOCKED_EXACT_EVENTS = frozenset(
    {
        "os.exec",
        "os.chmod",
        "os.chown",
        "os.link",
        "os.mkdir",
        "os.posix_spawn",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.spawn",
        "os.symlink",
        "os.system",
        "os.truncate",
        "os.unlink",
        "pty.spawn",
        "socket.bind",
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.getnameinfo",
        "socket.sendto",
        "subprocess.Popen",
    }
)


def _deny_expanded_capabilities(event: str, _args: tuple[object, ...]) -> None:
    if event in _BLOCKED_EXACT_EVENTS or event.startswith("subprocess."):
        raise PermissionError("Skill script capability is blocked")
    if event == "open" and _open_requests_write(_args):
        raise PermissionError("Skill script filesystem writes are blocked")


def _open_requests_write(args: tuple[object, ...]) -> bool:
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None
    if isinstance(mode, str) and any(value in mode for value in ("w", "a", "x", "+")):
        return True
    if isinstance(flags, int):
        write_flags = (
            os.O_WRONLY
            | os.O_RDWR
            | os.O_CREAT
            | os.O_TRUNC
            | os.O_APPEND
        )
        return bool(flags & write_flags)
    return False


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--script":
        return 64
    script = Path(sys.argv[2])
    forwarded = sys.argv[3:]
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    sys.argv = [str(script), *forwarded]
    sys.dont_write_bytecode = True
    sys.addaudithook(_deny_expanded_capabilities)
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
