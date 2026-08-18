"""WorkspacePickerPort — 系统目录选择器抽象端口。

Application Service 只依赖此接口，不依赖具体平台实现。
非 macOS 平台返回安全 AppError，不 fallback 到非可信路径。
测试注入 FakeWorkspacePicker，不在自动化测试中弹出真实系统窗口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PickerResult:
    """目录选择器返回结果。"""

    selected_path: str | None = None
    cancelled: bool = False
    error_code: str | None = None
    error_message: str | None = None


class WorkspacePickerPort(ABC):
    """系统目录选择器端口。"""

    @abstractmethod
    async def pick_directory(self) -> PickerResult:
        """弹出系统目录选择器，返回用户选择的目录路径。

        Returns:
            PickerResult:
            - selected_path 有值且 cancelled=False → 用户选择了目录
            - cancelled=True → 用户取消
            - error_code 有值 → 选择器执行失败
        """
        ...
