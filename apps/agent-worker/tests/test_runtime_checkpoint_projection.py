"""AgentRun 失败终态的模型重试 checkpoint 投影回归测试。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

import jarvis_worker.runtime.service as runtime_service_module
from jarvis_worker.runtime.events import build_envelope, build_runtime_event
from jarvis_worker.runtime.service import RuntimeApplicationService
from jarvis_worker.shared.domain.models import AgentRun, RunStatus, Task, TaskStatus, utcnow


async def _noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resume_node", "preserved"),
    [("extract_intent", True), ("call_model", True), ("validate_action", False)],
)
async def test_failed_run_preserves_only_supported_model_retry_checkpoint(
    monkeypatch, resume_node, preserved
):
    task_id, run_id, trace_id = uuid4(), uuid4(), uuid4()
    task = Task(
        id=task_id,
        title="retry",
        user_goal="retry model",
        conversation_id=uuid4(),
        status=TaskStatus.RUNNING,
    )
    checkpoint = {"resume_node": resume_node}
    run = AgentRun(
        id=run_id,
        task_id=task_id,
        status=RunStatus.RUNNING,
        checkpoint=checkpoint,
    )

    class Runs:
        async def update_with_lock(self, **_kwargs):
            return True

    class Tasks:
        async def update(self, _item):
            return None

    tx = SimpleNamespace(runs=Runs(), tasks=Tasks())
    event = build_runtime_event(
        "agent.run.failed",
        str(task_id),
        str(run_id),
        payload={
            "error": {
                "code": "MODEL_TIMEOUT",
                "message": "模型超时",
                "category": "model",
                "recoverable": True,
            }
        },
    )
    envelope = build_envelope(event, str(trace_id), "worker-test")
    service = RuntimeApplicationService(lambda: None)
    service._expire_pending_permissions = _noop
    service._fail_open_children = _noop
    monkeypatch.setattr(runtime_service_module, "is_resumable_run_checkpoint", lambda _value: True)

    await service._apply_projection(tx, envelope, run, task, task_id, run_id, None, utcnow())

    assert run.status is RunStatus.FAILED
    assert run.checkpoint == (checkpoint if preserved else {})
