"""MacOSWorkspacePickerAdapter — 使用 osascript 弹出系统目录选择器。

安全约束：
- 使用固定绝对路径 /usr/bin/osascript
- 使用固定 AppleScript（不拼接用户输入）
- shell=False
- 设置合理超时
- 使用 asyncio.create_subprocess_exec 支持 context cancellation
- cancel 后确保子进程被终止，不留后台系统窗口
"""

from __future__ import annotations

import asyncio
import logging
import os

from jarvis_worker.runtime.workspaces.workspace_picker import PickerResult, WorkspacePickerPort

logger = logging.getLogger(__name__)

# 固定 AppleScript：不拼接用户输入，不暴露路径
_APPLESCRIPT = """
tell application "System Events"
    activate
    set folderPath to choose folder with prompt "选择工作区目录："
    return POSIX path of folderPath
end tell
"""


def _is_user_cancellation(stderr_text: str) -> bool:
    """识别 AppleScript 的用户取消结果。

    ``osascript`` 的错误文案会随系统语言变化，但 AppleScript 的用户取消
    错误码始终为 ``-128``。同时保留英文和中文文案，便于覆盖不同 macOS
    版本输出。只有这些明确取消信号才被视为取消，其他非零退出仍保留为错误。
    """
    normalized = stderr_text.casefold()
    return (
        "-128" in normalized
        or "user canceled" in normalized
        or "user cancelled" in normalized
        or "用户取消" in normalized
    )


class MacOSWorkspacePickerAdapter(WorkspacePickerPort):
    """使用 /usr/bin/osascript 弹出目录选择器。

    通过 asyncio.create_subprocess_exec 执行，支持 context cancellation。
    """

    def __init__(self, timeout_seconds: float = 60.0):
        self._timeout = timeout_seconds

    async def pick_directory(self) -> PickerResult:
        """启动 osascript 子进程，等待用户选择或取消。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/osascript",
                "-e", _APPLESCRIPT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                # timeout：终止子进程
                logger.warning("Picker 超时，终止 osascript 进程")
                await self._kill_proc(proc)
                return PickerResult(
                    error_code="WORKSPACE_PICK_FAILED",
                    error_message="目录选择超时",
                )
            except asyncio.CancelledError:
                # context 取消：终止子进程
                logger.info("Picker 被取消，终止 osascript 进程")
                await self._kill_proc(proc)
                raise

            return self._parse_result(proc.returncode, stdout, stderr)

        except Exception as exc:
            logger.error("目录选择器执行失败: %s", exc)
            return PickerResult(
                error_code="WORKSPACE_PICK_FAILED",
                error_message="目录选择器执行失败",
            )

    async def _kill_proc(self, proc: asyncio.subprocess.Process) -> None:
        """确保子进程被终止。"""
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass  # 已退出

    @staticmethod
    def _parse_result(
        returncode: int | None,
        stdout: bytes | None,
        stderr: bytes | None,
    ) -> PickerResult:
        stdout_text = (stdout or b"").decode("utf-8", errors="replace").strip()
        stderr_text = (stderr or b"").decode("utf-8", errors="replace").strip()

        if returncode == 0 and stdout_text:
            if os.path.isdir(stdout_text):
                return PickerResult(selected_path=stdout_text)
            return PickerResult(
                error_code="WORKSPACE_PATH_FORBIDDEN",
                error_message="选择的路径不是有效目录",
            )

        # 用户取消（AppleScript error -128；文案会随系统语言变化）
        if returncode is not None and returncode != 0:
            if _is_user_cancellation(stderr_text):
                return PickerResult(cancelled=True)
            logger.warning(
                "osascript 非零退出: rc=%d stderr=%s", returncode, stderr_text,
            )
            return PickerResult(
                error_code="WORKSPACE_PICK_FAILED",
                error_message="目录选择器返回错误",
            )

        return PickerResult(
            error_code="WORKSPACE_PICK_FAILED",
            error_message="目录选择器未返回有效路径",
        )
