"""Permission Required MVP 测试 — 使用 fakeredis 模拟 Redis。

验证：
  - PermissionDecisionCommand 解码
  - permission scenario 发出 permission.required
  - approve decision 后继续完成
  - deny decision 后停止并报 failed
  - decision run_id 不匹配时不 ack
  - permission wait 超时后优雅退出
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from uuid import uuid4

import fakeredis
import pytest

from jarvis_worker.agent.core.checkpoint import (
    build_permission_checkpoint,
    validate_permission_checkpoint,
)
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.harness import RunBudget, RunSupervisor
from jarvis_worker.agent.loop import CompletionContract, LoopProgressSnapshot, StopDecision
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest
from jarvis_worker.runtime_bus.messages import (
    SCHEMA_VERSION,
    PermissionDecisionCommand,
    RunCancelCommand,
    RunJobMessage,
)
from jarvis_worker.shared.domain.models import (
    AgentRun,
    PermissionRequest,
    PermissionStatus,
    RunStatus,
)
from tests.testing_doubles import MockRunner

# -- PermissionDecisionCommand decode 测试 --


class TestPermissionDecisionCommandDecode:
    def test_decode_valid_allow_once(self):
        data = {
            "command_id": "cmd-001",
            "trace_id": "trace-001",
            "request_id": "req-001",
            "task_id": "task-001",
            "run_id": "run-001",
            "decision": "allow_once",
            "decided_at": "2026-07-07T10:00:00Z",
            "schema_version": SCHEMA_VERSION,
        }
        cmd = PermissionDecisionCommand.from_dict(data)
        assert cmd.request_id == "req-001"
        assert cmd.decision == "allow_once"
        assert cmd.run_id == "run-001"

    def test_decode_valid_deny(self):
        data = {
            "command_id": "cmd-002",
            "trace_id": "trace-002",
            "request_id": "req-002",
            "task_id": "task-002",
            "run_id": "run-002",
            "decision": "deny",
            "decided_at": "2026-07-07T10:00:00Z",
            "schema_version": SCHEMA_VERSION,
        }
        cmd = PermissionDecisionCommand.from_dict(data)
        assert cmd.decision == "deny"

    def test_decode_missing_request_id(self):
        data = {
            "command_id": "cmd-001",
            "trace_id": "trace-001",
            "task_id": "task-001",
            "run_id": "run-001",
            "decision": "allow_once",
            "decided_at": "2026-07-07T10:00:00Z",
            "schema_version": SCHEMA_VERSION,
        }
        with pytest.raises(ValueError, match="缺少必要字段"):
            PermissionDecisionCommand.from_dict(data)

    def test_decode_bad_schema_version(self):
        data = {
            "command_id": "cmd-001",
            "trace_id": "trace-001",
            "request_id": "req-001",
            "task_id": "task-001",
            "run_id": "run-001",
            "decision": "allow_once",
            "decided_at": "2026-07-07T10:00:00Z",
            "schema_version": "bad-ver",
        }
        with pytest.raises(ValueError, match="schema_version"):
            PermissionDecisionCommand.from_dict(data)

    def test_decode_with_optional_note(self):
        data = {
            "command_id": "cmd-001",
            "trace_id": "trace-001",
            "request_id": "req-001",
            "task_id": "task-001",
            "run_id": "run-001",
            "decision": "deny",
            "decided_at": "2026-07-07T10:00:00Z",
            "schema_version": SCHEMA_VERSION,
            "note": "不需要这个操作",
        }
        cmd = PermissionDecisionCommand.from_dict(data)
        assert cmd.note == "不需要这个操作"


def _permission_checkpoint(request_id, task_id, run_id, step_id, tool_call_id):
    job = RunJobMessage(
        job_id=str(uuid4()),
        trace_id=str(uuid4()),
        task_id=str(task_id),
        run_id=str(run_id),
        user_goal="写文件",
        created_at="2026-07-16T00:00:00Z",
    )
    tool_request = ToolRequest(
        task_id=str(task_id),
        run_id=str(run_id),
        step_id=str(step_id),
        tool_name="workspace.write_file",
        arguments={"path": "note.md", "content": "hello"},
    )
    state = AgentState(task_id=str(task_id), run_id=str(run_id), user_goal="写文件")
    state.completion_contract = CompletionContract(
        required_tool_names=("workspace.write_file",),
        workspace_action="write",
    ).to_state_dict()
    state.loop_progress = LoopProgressSnapshot().to_state_dict()
    state.stop_decision = StopDecision(
        disposition="continue",
        reason_code="LOOP_INITIALIZED",
    ).to_state_dict()
    RunSupervisor(RunBudget()).ensure_run_control(state)
    return build_permission_checkpoint(
        job=job,
        state=state,
        next_step_seq=4,
        permission_request_id=str(request_id),
        tool_request=tool_request,
        tool_call_base={
            "id": str(tool_call_id),
            "run_id": str(run_id),
            "step_id": str(step_id),
            "tool_name": tool_request.tool_name,
            "permission_request_id": str(request_id),
        },
        model_action={
            "action_type": "call_tool",
            "tool_name": tool_request.tool_name,
            "arguments": {"path": "note.md", "content": "hello"},
            "reason": "写文件",
        },
    )


def test_permission_checkpoint_rejects_persisted_identity_mismatch():
    request_id, task_id, run_id, step_id, tool_call_id = (uuid4() for _ in range(5))
    checkpoint = _permission_checkpoint(request_id, task_id, run_id, step_id, tool_call_id)

    validate_permission_checkpoint(
        checkpoint,
        expected_request_id=str(request_id),
        expected_task_id=str(task_id),
        expected_run_id=str(run_id),
        expected_step_id=str(step_id),
        expected_tool_call_id=str(tool_call_id),
        expected_tool_name="workspace.write_file",
    )
    with pytest.raises(ValueError, match="持久化身份不一致"):
        validate_permission_checkpoint(checkpoint, expected_request_id=str(uuid4()))

    checkpoint["tool_request"]["run_id"] = str(uuid4())
    with pytest.raises(ValueError, match="tool request 标识不一致"):
        validate_permission_checkpoint(checkpoint)


def test_idle_worker_resumes_only_persisted_matching_permission():
    """生产权限等待不依赖原 Worker 内存；空闲 Worker 可从 checkpoint 恢复。"""
    from jarvis_worker.runtime.worker import AgentWorker
    from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
    from jarvis_worker.runtime_bus.producer import RuntimeEventProducer

    request_id, task_id, run_id = uuid4(), uuid4(), uuid4()
    step_id, tool_call_id = uuid4(), uuid4()
    checkpoint = _permission_checkpoint(request_id, task_id, run_id, step_id, tool_call_id)
    req = PermissionRequest(
        id=request_id,
        task_id=task_id,
        run_id=run_id,
        tool_name="workspace.write_file",
        action_summary="写文件",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        allowed_decisions=["allow_once", "deny"],
        status=PermissionStatus.APPROVED,
        decision="allow_once",
        checkpoint=checkpoint,
        step_id=step_id,
        tool_call_id=tool_call_id,
    )

    class PermService:
        async def get_request(self, _request_id):
            return req

    class Bridge:
        def run(self, awaitable, timeout=None):
            return asyncio.run(awaitable)

    class Runner:
        worker_id = "worker-resume"
        resumed = None

        def resume_permission(
            self, checkpoint, decision, cancel_check=None, publish_cb=None
        ):
            self.resumed = (checkpoint, decision)
            return []

    class CommandConsumer:
        acked = []

        def read_one(self, block_ms=200):
            return None, None, None

        def ack(self, msg_id):
            self.acked.append(msg_id)
            return True

    client = fakeredis.FakeRedis(decode_responses=True)
    runner = Runner()
    command_consumer = CommandConsumer()
    worker = AgentWorker(
        client,
        RunQueueConsumer(client, "test-resume"),
        RuntimeEventProducer(client),
        runner,
        cmd_consumer=command_consumer,
        perm_service=PermService(),
    )
    worker._service_bridge = Bridge()
    cmd = PermissionDecisionCommand(
        command_id=str(uuid4()),
        trace_id=str(uuid4()),
        request_id=str(request_id),
        task_id=str(task_id),
        run_id=str(run_id),
        decision="allow_once",
        decided_at="2026-07-16T00:00:00Z",
    )

    worker._resume_permission_command(cmd, "redis-command-1")

    assert runner.resumed == (checkpoint, "allow_once")
    assert command_consumer.acked == ["redis-command-1"]
    assert worker._get_active_run_id() == ""

    req.checkpoint = deepcopy(checkpoint)
    req.checkpoint["permission_request_id"] = str(uuid4())
    runner.resumed = None
    worker._resume_permission_command(cmd, "redis-command-tampered")

    assert runner.resumed is None
    assert command_consumer.acked == ["redis-command-1", "redis-command-tampered"]

    req.status = PermissionStatus.EXPIRED
    worker._resume_permission_command(cmd, "redis-command-expired")

    assert runner.resumed is None
    assert command_consumer.acked == [
        "redis-command-1",
        "redis-command-tampered",
        "redis-command-expired",
    ]


def test_idle_worker_fails_closed_for_legacy_permission_checkpoint():
    """旧 Step ID 语义的授权检查点不得执行已批准工具。"""
    from jarvis_worker.runtime.worker import AgentWorker
    from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
    from jarvis_worker.runtime_bus.producer import RuntimeEventProducer

    request_id, task_id, run_id = uuid4(), uuid4(), uuid4()
    req = PermissionRequest(
        id=request_id,
        task_id=task_id,
        run_id=run_id,
        tool_name="workspace.write_file",
        action_summary="写文件",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        allowed_decisions=["allow_once", "deny"],
        status=PermissionStatus.APPROVED,
        decision="allow_once",
        checkpoint={"version": 1},
    )

    class PermService:
        async def get_request(self, _request_id):
            return req

    class Bridge:
        def run(self, awaitable, timeout=None):
            return asyncio.run(awaitable)

    class Runner:
        worker_id = "worker-resume"
        resumed = False

        def resume_permission(
            self, checkpoint, decision, cancel_check=None, publish_cb=None
        ):
            self.resumed = True
            return []

    class CommandConsumer:
        acked = []

        def read_one(self, block_ms=200):
            return None, None, None

        def ack(self, msg_id):
            self.acked.append(msg_id)
            return True

    client = fakeredis.FakeRedis(decode_responses=True)
    runner = Runner()
    command_consumer = CommandConsumer()
    worker = AgentWorker(
        client,
        RunQueueConsumer(client, "test-legacy-resume"),
        RuntimeEventProducer(client),
        runner,
        cmd_consumer=command_consumer,
        perm_service=PermService(),
    )
    worker._service_bridge = Bridge()
    cmd = PermissionDecisionCommand(
        command_id=str(uuid4()),
        trace_id=str(uuid4()),
        request_id=str(request_id),
        task_id=str(task_id),
        run_id=str(run_id),
        decision="allow_once",
        decided_at="2026-07-30T00:00:00Z",
    )

    worker._resume_permission_command(cmd, "redis-command-legacy")

    assert runner.resumed is False
    assert command_consumer.acked == ["redis-command-legacy"]


def test_idle_cancel_command_acks_terminal_run_only_when_identity_matches():
    from jarvis_worker.runtime.worker import AgentWorker
    from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
    from jarvis_worker.runtime_bus.producer import RuntimeEventProducer

    task_id, run_id = uuid4(), uuid4()
    run = AgentRun(id=run_id, task_id=task_id, status=RunStatus.CANCELLED)

    class RunService:
        async def get_run(self, _run_id):
            return run

    class Bridge:
        def run(self, awaitable, timeout=None):
            return asyncio.run(awaitable)

    class Runner:
        worker_id = "worker-cancel"

    class CommandConsumer:
        def __init__(self):
            self.acked = []

        def ack(self, msg_id):
            self.acked.append(msg_id)
            return True

    client = fakeredis.FakeRedis(decode_responses=True)
    command_consumer = CommandConsumer()
    worker = AgentWorker(
        client,
        RunQueueConsumer(client, "test-terminal-cancel"),
        RuntimeEventProducer(client),
        Runner(),
        cmd_consumer=command_consumer,
        run_service=RunService(),
        service_bridge=Bridge(),
    )
    published = []
    worker._publish_cancelled_event = lambda job: published.append(job)
    cmd = RunCancelCommand(
        command_id=str(uuid4()),
        trace_id=str(uuid4()),
        task_id=str(task_id),
        run_id=str(run_id),
        requested_at="2026-08-03T00:00:00Z",
    )

    worker._cancel_idle_run(cmd, "redis-cancel-terminal")

    assert command_consumer.acked == ["redis-cancel-terminal"]
    assert published == []

    cmd.task_id = str(uuid4())
    worker._cancel_idle_run(cmd, "redis-cancel-mismatched")
    assert command_consumer.acked == ["redis-cancel-terminal"]


@pytest.mark.parametrize(
    ("decision", "status", "expected_code"),
    [
        ("allow_once", PermissionStatus.APPROVED, "PERMISSION_RESUME_EFFECT_UNKNOWN"),
        ("deny", PermissionStatus.DENIED, "PERMISSION_DENIAL_INTERRUPTED"),
    ],
)
def test_idle_worker_fails_closed_when_permission_resume_lease_is_stale(
    decision, status, expected_code
):
    from jarvis_worker.runtime.worker import AgentWorker
    from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
    from jarvis_worker.runtime_bus.producer import RuntimeEventProducer

    request_id, task_id, run_id, step_id, tool_call_id = (uuid4() for _ in range(5))
    checkpoint = _permission_checkpoint(request_id, task_id, run_id, step_id, tool_call_id)
    req = PermissionRequest(
        id=request_id,
        task_id=task_id,
        run_id=run_id,
        step_id=step_id,
        tool_call_id=tool_call_id,
        tool_name="workspace.write_file",
        action_summary="写文件",
        risk_level="L2",
        scope={"type": "once"},
        arguments_summary={},
        allowed_decisions=["allow_once", "deny"],
        status=status,
        decision=decision,
        checkpoint=checkpoint,
    )

    class PermService:
        async def get_request(self, _request_id):
            return req

    class RunService:
        async def claim_permission_resume(self, _run_id, _worker_id):
            return object(), "stale"

    class Bridge:
        def run(self, awaitable, timeout=None):
            return asyncio.run(awaitable)

    class Runner:
        worker_id = "worker-resume"
        resumed = False

        def resume_permission(self, *_args, **_kwargs):
            self.resumed = True
            return []

    class CommandConsumer:
        def __init__(self):
            self.acked = []

        def ack(self, msg_id):
            self.acked.append(msg_id)
            return True

    client = fakeredis.FakeRedis(decode_responses=True)
    runner = Runner()
    command_consumer = CommandConsumer()
    worker = AgentWorker(
        client,
        RunQueueConsumer(client, f"test-stale-{decision}"),
        RuntimeEventProducer(client),
        runner,
        cmd_consumer=command_consumer,
        run_service=RunService(),
        perm_service=PermService(),
        service_bridge=Bridge(),
    )
    failures = []
    worker._publish_failed_event = lambda _job, **kwargs: failures.append(kwargs)
    cmd = PermissionDecisionCommand(
        command_id=str(uuid4()),
        trace_id=str(uuid4()),
        request_id=str(request_id),
        task_id=str(task_id),
        run_id=str(run_id),
        decision=decision,
        decided_at="2026-08-03T00:00:00Z",
    )

    worker._resume_permission_command(cmd, "redis-command-stale")

    assert runner.resumed is False
    assert command_consumer.acked == ["redis-command-stale"]
    assert failures[0]["code"] == expected_code
    assert failures[0]["recoverable"] is False


# -- Mock runner permission scenario 测试 --


class TestPermissionScenario:
    def test_permission_required_event_emitted(self):
        """permission 场景发出 permission.required RuntimeEvent。"""
        runner = MockRunner(worker_id="test-worker")
        job = RunJobMessage(
            job_id="job-perm",
            trace_id="trace-perm",
            task_id="task-perm",
            run_id="run-perm",
            user_goal="测试 permission 场景",
            created_at="2026-07-07T10:00:00Z",
        )

        # 模拟 approve decision
        def wait_decision(req_id):
            return "allow_once"

        envelopes = runner.run_with_cancel_check(job, wait_decision=wait_decision)
        event_types = [e.event_type for e in envelopes]
        assert "permission.required" in event_types
        # 验证 payload shape
        perm_event = next(e for e in envelopes if e.event_type == "permission.required")
        req = perm_event.runtime_event["payload"]["request"]
        assert req["tool_name"] == "shell"
        assert req["risk_level"] == "L3"
        assert "allow_once" in req["allowed_decisions"]
        assert "deny" in req["allowed_decisions"]

    def test_approve_continues_to_completed(self):
        """approve → permission.resolved → tool.call.finished → agent.run.completed。"""
        runner = MockRunner(worker_id="test-worker")
        job = RunJobMessage(
            job_id="job-approve",
            trace_id="trace-approve",
            task_id="task-approve",
            run_id="run-approve",
            user_goal="permission approve test",
            created_at="2026-07-07T10:00:00Z",
        )
        envelopes = runner.run_with_cancel_check(
            job,
            wait_decision=lambda rid: "allow_once",
        )
        types = [e.event_type for e in envelopes]
        assert types == [
            "agent.run.started",
            "tool.call.started",
            "permission.required",
            "permission.resolved",
            "tool.call.finished",
            "agent.run.completed",
        ]

    def test_deny_stops_with_failed(self):
        """deny → permission.resolved → tool.call.failed → agent.run.failed。"""
        runner = MockRunner(worker_id="test-worker")
        job = RunJobMessage(
            job_id="job-deny",
            trace_id="trace-deny",
            task_id="task-deny",
            run_id="run-deny",
            user_goal="权限 deny 测试",
            created_at="2026-07-07T10:00:00Z",
        )
        envelopes = runner.run_with_cancel_check(
            job,
            wait_decision=lambda rid: "deny",
        )
        types = [e.event_type for e in envelopes]
        assert types == [
            "agent.run.started",
            "tool.call.started",
            "permission.required",
            "permission.resolved",
            "tool.call.failed",
            "agent.run.failed",
        ]
        # 验证 permission.resolved payload
        resolved = envelopes[3]
        assert resolved.runtime_event["payload"]["decision"] == "deny"
        # deny 时 tool.call.failed 错误码是 PERMISSION_DENIED
        failed = envelopes[4]
        err = failed.runtime_event["payload"]["tool_call"]["error"]
        assert err["code"] == "PERMISSION_DENIED"

    def test_timeout_produces_failed_no_resolved(self):
        """超时 → 不生成 permission.resolved，tool.call.failed=PERMISSION_TIMEOUT。"""
        runner = MockRunner(worker_id="test-worker")
        job = RunJobMessage(
            job_id="job-timeout",
            trace_id="trace-timeout",
            task_id="task-timeout",
            run_id="run-timeout",
            user_goal="permission timeout test",
            created_at="2026-07-07T10:00:00Z",
        )
        envelopes = runner.run_with_cancel_check(
            job,
            wait_decision=lambda rid: None,  # 超时
        )
        types = [e.event_type for e in envelopes]
        assert types == [
            "agent.run.started",
            "tool.call.started",
            "permission.required",
            # 无 permission.resolved
            "tool.call.failed",
            "agent.run.failed",
        ]
        # timeout 时 tool.call.failed 错误码是 PERMISSION_TIMEOUT
        failed = envelopes[3]
        err = failed.runtime_event["payload"]["tool_call"]["error"]
        assert err["code"] == "PERMISSION_TIMEOUT"
        assert "超时" in err.get("message", "")
        # agent.run.failed 也标记 timeout
        run_failed = envelopes[4]
        assert "PERMISSION_TIMEOUT" in str(run_failed.runtime_event["payload"])

    def test_prepare_wait_called_before_publish(self):
        """prepare_wait 在 publish_cb 之前调用，防止 decision 早到竞态。"""
        runner = MockRunner(worker_id="test-worker")
        job = RunJobMessage(
            job_id="job-race",
            trace_id="trace-race",
            task_id="task-race",
            run_id="run-race",
            user_goal="permission race test",
            created_at="2026-07-07T10:00:00Z",
        )
        call_order = []

        envelopes = runner.run_with_cancel_check(
            job,
            prepare_wait=lambda rid: call_order.append("prepare"),
            publish_cb=lambda env: call_order.append("publish"),
            wait_decision=lambda rid: (call_order.append("wait"), "allow_once")[1],
        )
        # prepare 必须在 publish 之前
        assert call_order[0] == "prepare", f"prepare 应先于 publish: {call_order}"
        assert call_order[1] == "publish", f"publish 应在 prepare 之后: {call_order}"
        assert "agent.run.completed" in [e.event_type for e in envelopes]

    def test_decision_in_window_not_overwritten(self):
        """decision 在 prepare→wait 窗口内到达不被 wait 覆盖，应正确返回。"""
        import fakeredis

        from jarvis_worker.runtime.worker import AgentWorker
        from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
        from jarvis_worker.runtime_bus.producer import RuntimeEventProducer

        client = fakeredis.FakeRedis(decode_responses=True)
        consumer = RunQueueConsumer(client, "test")
        producer = RuntimeEventProducer(client)
        runner = MockRunner(worker_id="test")

        worker = AgentWorker(client, consumer, producer, runner)

        # 模拟 prepare → decision 到达 → wait 的场景
        request_id = "req-race-001"
        worker._prepare_permission_wait(request_id)

        # 模拟 command poll thread 在 wait 之前收到了 matching decision
        with worker._perm_lock:
            worker._perm_decision = "allow_once"
            worker._perm_received.set()

        # wait 应立即返回已到达的 decision，不覆盖
        result = worker._wait_permission_decision(request_id, timeout_s=1.0)
        assert result == "allow_once", f"不应丢失窗口内到达的 decision，got {result}"

    def test_wrong_request_id_in_window_not_returned(self):
        """窗口内到达的 decision 但 request_id 不对 → 不返回。"""
        import fakeredis

        from jarvis_worker.runtime.worker import AgentWorker
        from jarvis_worker.runtime_bus.consumer import RunQueueConsumer
        from jarvis_worker.runtime_bus.producer import RuntimeEventProducer

        client = fakeredis.FakeRedis(decode_responses=True)
        consumer = RunQueueConsumer(client, "test")
        producer = RuntimeEventProducer(client)
        runner = MockRunner(worker_id="test")

        worker = AgentWorker(client, consumer, producer, runner)
        worker._prepare_permission_wait("req-correct")

        # 窗口内收到 wrong request_id 的 decision
        with worker._perm_lock:
            worker._perm_request_id = "req-wrong"
            worker._perm_decision = "allow_once"
            worker._perm_received.set()

        # wait 应 fallback 初始化 request_id，然后超时（因为 wrong decision 不匹配）
        # 注意：_prepare_permission_wait 设置了 request_id="req-correct"，
        # 这里我们又覆盖为 "req-wrong" + allow_once。
        # _wait_permission_decision 检查 cur_req("req-wrong") != request_id("req-correct")，
        # 所以会 fallback 初始化成 request_id="req-correct"，丢弃 wrong decision。
        result = worker._wait_permission_decision("req-correct", timeout_s=0.3)
        assert result is None, f"wrong request_id 不应解除等待，got {result}"

    def test_normal_scenario_not_affected(self):
        """普通 user_goal 不走 permission 场景。"""
        runner = MockRunner(worker_id="test-worker")
        job = RunJobMessage(
            job_id="job-normal",
            trace_id="trace-normal",
            task_id="task-normal",
            run_id="run-normal",
            user_goal="普通任务",
            created_at="2026-07-07T10:00:00Z",
        )
        envelopes = runner.run_with_cancel_check(job, wait_decision=lambda rid: "allow_once")
        types = [e.event_type for e in envelopes]
        assert "permission.required" not in types
        assert types[-1] == "agent.run.completed"
