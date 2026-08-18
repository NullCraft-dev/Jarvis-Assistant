"""生产 AgentRunExecutor 的窄适配测试。"""

from __future__ import annotations

from jarvis_worker.runtime_bus.messages import RunJobMessage
from jarvis_worker.runtime.run_executor import AgentRunExecutor


def test_agent_run_executor_delegates_to_agent_runner() -> None:
    calls: list[tuple[RunJobMessage, str, object]] = []

    class FakeAgentRunner:
        def run(self, job, default_workspace_root="", cancel_check=None, history_messages=None, **kwargs):
            calls.append((job, default_workspace_root, cancel_check))
            return []

    job = RunJobMessage(
        job_id="job-1",
        trace_id="trace-1",
        task_id="task-1",
        run_id="run-1",
        user_goal="列出工作区文件",
        created_at="2026-07-11T00:00:00Z",
    )
    cancel_check = lambda: False
    executor = AgentRunExecutor(
        agent_runner=FakeAgentRunner(),  # type: ignore[arg-type]
        worker_id="worker-1",
        default_workspace_root="/workspace",
    )

    result = executor.run_with_cancel_check(
        job,
        cancel_check=cancel_check,
        wait_decision=lambda _request_id: "allow_once",
        publish_cb=lambda _event: None,
        prepare_wait=lambda _request_id: None,
    )

    assert result == []
    assert executor.worker_id == "worker-1"
    assert calls == [(job, "/workspace", cancel_check)]


def test_agent_run_executor_delegates_checkpoint_resume() -> None:
    calls = []

    class FakeAgentRunner:
        def resume_from_checkpoint(self, checkpoint, cancel_check=None, publish_cb=None):
            calls.append((checkpoint, cancel_check, publish_cb))
            return []

    cancel_check = lambda: False
    publish_cb = lambda _event: None
    executor = AgentRunExecutor(
        agent_runner=FakeAgentRunner(),  # type: ignore[arg-type]
        worker_id="worker-1",
    )

    assert executor.resume_from_checkpoint(
        {"version": 1}, cancel_check=cancel_check, publish_cb=publish_cb
    ) == []
    assert calls == [({"version": 1}, cancel_check, publish_cb)]
