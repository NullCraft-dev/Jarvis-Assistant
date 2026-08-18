"""UnsupportedWorkspacePickerAdapter — 非 macOS 平台的安全 fallback。

不尝试非可信路径兜底，始终返回安全 AppError。
"""

from jarvis_worker.runtime.workspaces.workspace_picker import PickerResult, WorkspacePickerPort


class UnsupportedWorkspacePickerAdapter(WorkspacePickerPort):
    """非 macOS 平台的选择器适配器 — 始终返回不可用错误。"""

    async def pick_directory(self) -> PickerResult:
        return PickerResult(
            error_code="WORKSPACE_PICKER_UNAVAILABLE",
            error_message="当前平台不支持系统目录选择器",
        )
