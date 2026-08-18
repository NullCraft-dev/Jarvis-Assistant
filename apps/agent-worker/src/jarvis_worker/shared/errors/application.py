"""Application 层结构化错误。

所有 Application Service 错误使用以下类型，不抛出原始异常。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AppError(Exception):
    """应用层结构化错误。

    面向 UI 和日志展示。不包含密钥、token、密码或数据库连接串。
    """
    code: str
    message: str
    category: str = "runtime"
    recoverable: bool = False
    details: dict = field(default_factory=dict)
    cause_id: Optional[str] = None

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# ── 通用错误 ──

def database_unavailable(detail: str = "") -> AppError:
    return AppError(
        code="DATABASE_UNAVAILABLE",
        message="数据库不可用",
        category="storage",
        recoverable=True,
        details={"error": detail} if detail else {},
    )


def not_found(entity: str, entity_id: str) -> AppError:
    return AppError(
        code="NOT_FOUND",
        message=f"{entity} 不存在: {entity_id}",
        category="not_found",
        recoverable=False,
    )


def validation_error(message: str) -> AppError:
    return AppError(
        code="VALIDATION_ERROR",
        message=message,
        category="validation",
        recoverable=False,
    )


# ── 任务相关 ──

def task_create_failed(detail: str = "") -> AppError:
    return AppError(
        code="TASK_CREATE_FAILED",
        message=f"创建任务失败: {detail}" if detail else "创建任务失败",
        category="storage",
        recoverable=False,
    )


# ── 运行相关 ──

def invalid_state_transition(current: str, target: str) -> AppError:
    return AppError(
        code="INVALID_STATE_TRANSITION",
        message=f"非法状态迁移: {current} → {target}",
        category="validation",
        recoverable=False,
    )


def run_version_conflict(run_id: str) -> AppError:
    return AppError(
        code="RUN_VERSION_CONFLICT",
        message=f"运行状态已被修改，请重试: {run_id}",
        category="runtime",
        recoverable=True,
    )


def run_already_terminal(run_id: str, status: str) -> AppError:
    return AppError(
        code="RUN_ALREADY_TERMINAL",
        message=f"运行已处于终态: {run_id} ({status})",
        category="validation",
        recoverable=False,
    )


# ── 权限相关 ──

def permission_conflict(request_id: str, existing: str, requested: str) -> AppError:
    return AppError(
        code="PERMISSION_CONFLICT",
        message=f"权限决策冲突: 已决定为 {existing}，新请求为 {requested}",
        category="permission",
        recoverable=False,
        details={"request_id": request_id, "existing": existing, "requested": requested},
    )


def permission_not_pending(request_id: str, status: str) -> AppError:
    return AppError(
        code="PERMISSION_NOT_PENDING",
        message=f"权限请求已不可处理: {request_id} ({status})",
        category="permission",
        recoverable=False,
        details={"request_id": request_id, "status": status},
    )
