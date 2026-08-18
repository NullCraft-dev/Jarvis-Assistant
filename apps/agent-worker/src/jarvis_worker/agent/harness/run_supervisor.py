"""AgentRun 级运行控制与终态不变量。

该模块是 Harness owner，不参与模型决策、工具选择或业务状态持久化。它统一收敛
运行预算、取消信号和 Runner 停止边界；可恢复预算状态由 checkpoint v5 持久化。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope

MAX_TOOL_ITERATIONS = 20
MAX_MODEL_CALLS = 100
MAX_RUN_SECONDS = 86_400
RUN_CONTROL_VERSION = "run-control-v1"

_RUN_TERMINAL_EVENT_TYPES = frozenset(
    {
        "agent.run.completed",
        "agent.run.failed",
        "agent.run.cancelled",
    }
)
_RUN_SUSPENSION_EVENT_TYPES = frozenset(
    {
        "agent.run.paused",
        "permission.required",
    }
)
_RUN_STOP_EVENT_TYPES = _RUN_TERMINAL_EVENT_TYPES | _RUN_SUSPENSION_EVENT_TYPES


@dataclass(frozen=True, slots=True)
class RunBudget:
    """单次 AgentRun 的有界执行预算。"""

    max_tool_iterations: int = 3
    max_model_calls: int = 16
    max_run_seconds: int = 900

    def __post_init__(self) -> None:
        value = self.max_tool_iterations
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= MAX_TOOL_ITERATIONS
        ):
            raise ValueError(f"max_tool_iterations 必须在 1..{MAX_TOOL_ITERATIONS} 之间")
        if (
            not isinstance(self.max_model_calls, int)
            or isinstance(self.max_model_calls, bool)
            or not 1 <= self.max_model_calls <= MAX_MODEL_CALLS
        ):
            raise ValueError(f"max_model_calls 必须在 1..{MAX_MODEL_CALLS} 之间")
        if (
            not isinstance(self.max_run_seconds, int)
            or isinstance(self.max_run_seconds, bool)
            or not 1 <= self.max_run_seconds <= MAX_RUN_SECONDS
        ):
            raise ValueError(f"max_run_seconds 必须在 1..{MAX_RUN_SECONDS} 之间")


@dataclass(frozen=True, slots=True)
class RunControlState:
    """跨恢复持久化的 Harness 预算快照。"""

    started_at: str
    deadline_at: str
    max_tool_iterations: int
    max_model_calls: int
    max_run_seconds: int
    model_calls_used: int = 0
    version: str = RUN_CONTROL_VERSION

    def to_state_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_state_dict(cls, value: object) -> "RunControlState":
        expected = {
            "started_at",
            "deadline_at",
            "max_tool_iterations",
            "max_model_calls",
            "max_run_seconds",
            "model_calls_used",
            "version",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("run control state 结构无效")
        budget = RunBudget(
            max_tool_iterations=value["max_tool_iterations"],
            max_model_calls=value["max_model_calls"],
            max_run_seconds=value["max_run_seconds"],
        )
        model_calls_used = value["model_calls_used"]
        if (
            value["version"] != RUN_CONTROL_VERSION
            or not isinstance(model_calls_used, int)
            or isinstance(model_calls_used, bool)
            or not 0 <= model_calls_used <= budget.max_model_calls
        ):
            raise ValueError("run control state 字段无效")
        started_at = _parse_utc(value["started_at"], "started_at")
        deadline_at = _parse_utc(value["deadline_at"], "deadline_at")
        if deadline_at <= started_at:
            raise ValueError("run control deadline 无效")
        return cls(
            started_at=started_at.isoformat(),
            deadline_at=deadline_at.isoformat(),
            max_tool_iterations=budget.max_tool_iterations,
            max_model_calls=budget.max_model_calls,
            max_run_seconds=budget.max_run_seconds,
            model_calls_used=model_calls_used,
        )


@dataclass(frozen=True, slots=True)
class RunHaltDecision:
    code: str
    message: str


class CancellationController:
    """把外部取消探针转换为单调信号。

    一旦观察到取消，后续检查永远返回 True，避免底层 Redis/内存探针短暂抖动后
    又允许新的模型或工具动作开始。
    """

    def __init__(self, probe: Callable[[], bool] | None = None) -> None:
        self._probe = probe
        self._cancelled = False

    def is_cancelled(self) -> bool:
        if self._cancelled:
            return True
        if self._probe is not None and bool(self._probe()):
            self._cancelled = True
        return self._cancelled


class RuntimeInvariantViolation(RuntimeError):
    """Runner 产出违反 Harness 停止边界。"""


class RunSupervisor:
    """AgentRun 级 Harness 控制面。

    当前负责：
    - 拥有并校验工具迭代预算；
    - 为每次执行绑定单调取消控制器；
    - 校验 Runner 返回序列只能有一个终态，或以未决挂起边界结束。
    """

    def __init__(
        self,
        budget: RunBudget,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._budget = budget
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def budget(self) -> RunBudget:
        return self._budget

    def bind_cancellation(self, probe: Callable[[], bool] | None) -> CancellationController:
        return CancellationController(probe)

    def ensure_run_control(self, state: Any) -> RunControlState:
        raw = getattr(state, "run_control", None)
        if raw is not None:
            return RunControlState.from_state_dict(raw)
        started_at = self._utc_now()
        control = RunControlState(
            started_at=started_at.isoformat(),
            deadline_at=(started_at + timedelta(seconds=self._budget.max_run_seconds)).isoformat(),
            max_tool_iterations=self._budget.max_tool_iterations,
            max_model_calls=self._budget.max_model_calls,
            max_run_seconds=self._budget.max_run_seconds,
        )
        state.run_control = control.to_state_dict()
        return control

    def before_phase(self, state: Any) -> RunHaltDecision | None:
        control = self.ensure_run_control(state)
        if self._utc_now() >= _parse_utc(control.deadline_at, "deadline_at"):
            return RunHaltDecision(
                code="RUN_DEADLINE_EXCEEDED",
                message="AgentRun 已超过持久化 wall-clock deadline",
            )
        return None

    def before_model_call(self, state: Any) -> RunHaltDecision | None:
        halted = self.before_phase(state)
        if halted is not None:
            return halted
        control = RunControlState.from_state_dict(state.run_control)
        if control.model_calls_used >= control.max_model_calls:
            return RunHaltDecision(
                code="MODEL_CALL_BUDGET_EXHAUSTED",
                message="AgentRun 已耗尽模型调用预算",
            )
        state.run_control = RunControlState(
            started_at=control.started_at,
            deadline_at=control.deadline_at,
            max_tool_iterations=control.max_tool_iterations,
            max_model_calls=control.max_model_calls,
            max_run_seconds=control.max_run_seconds,
            model_calls_used=control.model_calls_used + 1,
        ).to_state_dict()
        return None

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("RunSupervisor clock 必须返回带时区时间")
        return value.astimezone(timezone.utc)

    @staticmethod
    def is_stop_event(event_type: str) -> bool:
        return event_type in _RUN_STOP_EVENT_TYPES

    @staticmethod
    def is_terminal_event(event_type: str) -> bool:
        return event_type in _RUN_TERMINAL_EVENT_TYPES

    def validate_result(self, envelopes: Sequence[RuntimeEventEnvelope]) -> None:
        """验证一次 Runner 调用的停止边界，发现漂移时失败关闭。"""
        if not envelopes:
            raise RuntimeInvariantViolation("AgentRunner 未产生任何 RuntimeEvent")

        terminal_indexes = [
            index
            for index, envelope in enumerate(envelopes)
            if self.is_terminal_event(envelope.event_type)
        ]
        if len(terminal_indexes) > 1:
            raise RuntimeInvariantViolation(
                f"AgentRunner 产生了多个终态事件: {len(terminal_indexes)}"
            )
        if terminal_indexes:
            if terminal_indexes[0] != len(envelopes) - 1:
                raise RuntimeInvariantViolation("终态事件之后仍产生了 RuntimeEvent")
            return

        if envelopes[-1].event_type not in _RUN_SUSPENSION_EVENT_TYPES:
            raise RuntimeInvariantViolation("AgentRunner 缺少终态或未决挂起边界")


def _parse_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"run control {field_name} 无效")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"run control {field_name} 无效") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"run control {field_name} 缺少时区")
    return parsed.astimezone(timezone.utc)
