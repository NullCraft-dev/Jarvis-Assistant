"""Worker 与 AgentRunner 之间的生产执行适配器。

多轮对话 MVP：历史作为 run 的局部参数传入，不使用跨调用可变暂存。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from jarvis_worker.agent.core.runner import AgentRunner
from jarvis_worker.runtime_bus.messages import RunJobMessage, RuntimeEventEnvelope


class RunExecutor(Protocol):
    """AgentWorker 所需的最小执行协议。"""

    @property
    def worker_id(self) -> str:
        """返回执行器所属 worker id。"""
        ...

    def run_with_cancel_check(
        self,
        job: RunJobMessage,
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], str | None] | None = None,
        wait_decision: Callable[[str], str | None] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
        prepare_wait: Callable[[str], None] | None = None,
        history_messages: list[dict[str, str]] | None = None,
        trusted_history_provenance: list[dict[str, str]] | None = None,
        memory_items: list[dict[str, Any]] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        """执行单次 run，并在安全点响应取消。

        history_messages 为本次 run 的会话历史（局部参数，不跨调用残留）。
        """
        ...

    def resume_permission(
        self,
        checkpoint: dict[str, Any],
        decision: str,
        cancel_check: Callable[[], bool] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        """从持久化权限检查点恢复 run。"""
        ...

    def resume_from_checkpoint(
        self,
        checkpoint: dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], str | None] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        """从 PostgreSQL Run checkpoint 恢复安全节点。"""
        ...


class AgentRunExecutor:
    """把生产 RunJobMessage 直接交给 AgentRunner。

    多轮对话：history_messages 作为 run_with_cancel_check 的局部参数，
    不通过实例可变字段跨调用暂存。取消/失败/异常后不会残留到下一 job。
    """

    def __init__(
        self,
        agent_runner: AgentRunner,
        worker_id: str,
        default_workspace_root: str = "",
    ) -> None:
        self._agent_runner = agent_runner
        self._worker_id = worker_id
        self._default_workspace_root = default_workspace_root

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def run_with_cancel_check(
        self,
        job: RunJobMessage,
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], str | None] | None = None,
        wait_decision: Callable[[str], str | None] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
        prepare_wait: Callable[[str], None] | None = None,
        history_messages: list[dict[str, str]] | None = None,
        trusted_history_provenance: list[dict[str, str]] | None = None,
        memory_items: list[dict[str, Any]] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        return self._agent_runner.run(
            job,
            default_workspace_root=self._default_workspace_root,
            cancel_check=cancel_check,
            pause_check=pause_check,
            history_messages=history_messages,
            trusted_history_provenance=trusted_history_provenance,
            memory_items=memory_items,
            prepare_wait=prepare_wait,
            wait_decision=wait_decision,
            publish_cb=publish_cb,
            defer_permission=True,
        )

    def resume_permission(
        self,
        checkpoint: dict[str, Any],
        decision: str,
        cancel_check: Callable[[], bool] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        return self._agent_runner.resume_permission(
            checkpoint,
            decision,
            cancel_check=cancel_check,
            publish_cb=publish_cb,
        )

    def resume_from_checkpoint(
        self,
        checkpoint: dict[str, Any],
        cancel_check: Callable[[], bool] | None = None,
        pause_check: Callable[[], str | None] | None = None,
        publish_cb: Callable[[RuntimeEventEnvelope], None] | None = None,
    ) -> list[RuntimeEventEnvelope]:
        kwargs = {"cancel_check": cancel_check, "publish_cb": publish_cb}
        if pause_check is not None:
            kwargs["pause_check"] = pause_check
        return self._agent_runner.resume_from_checkpoint(checkpoint, **kwargs)
