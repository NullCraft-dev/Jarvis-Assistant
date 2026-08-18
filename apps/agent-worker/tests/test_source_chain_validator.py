from __future__ import annotations

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.source_chain_validator import (
    WorkspaceSourceChainCoverageValidator,
)
from jarvis_worker.agent.core.state import AgentState


def _read(path: str, content: str | None = None) -> dict:
    normalized = path.casefold()
    if content is None:
        if "/web/" in normalized or normalized.startswith("apps/web"):
            content = 'return apiPost<CreateTaskOutput>("/tasks", input)'
        elif "gateway" in normalized:
            content = "response = h.controlPlane.CreateTask(ctx, request)"
        elif "outbox" in normalized or "publisher" in normalized:
            content = 'EVENT_TO_STREAM = {"task.created": "jarvis:stream:run-queue"}'
        elif "consumer" in normalized or "runtime_bus" in normalized:
            content = 'client.xreadgroup(streams={STREAM_RUN_QUEUE: ">"})'
        elif "worker" in normalized or "runner" in normalized:
            content = "self._process_job_with_cancel_check(job)"
        else:
            content = "caller.invoke(target)"
    return {
        "tool_name": "workspace.read_file",
        "ok": True,
        "data": {"path": path, "content": content},
    }


def _state(*paths: str) -> AgentState:
    return AgentState(
        user_goal="请阅读这个代码库，说明 Web 创建任务后直到 Worker 执行的真实调用链。",
        observations=[_read(path) for path in paths],
    )


def test_validator_rejects_finish_when_only_worker_half_has_source_evidence() -> None:
    validator = WorkspaceSourceChainCoverageValidator()
    state = _state(
        "apps/agent-worker/src/runtime_bus/consumer.py",
        "apps/agent-worker/src/runtime/worker.py",
    )

    result = validator.validate(action=AgentAction.finish("完成"), state=state)

    assert result.accepted is False
    assert "运行端只覆盖 1/2" in result.feedback
    assert "入口/传输/执行阶段只覆盖 1/3" in result.feedback
    assert "Web/前端入口" in result.feedback
    assert "入口" in result.feedback
    assert "apps/agent-worker" not in result.feedback
    assert result.reason_code == "SOURCE_CHAIN_EVIDENCE_INCOMPLETE"
    assert result.diagnostics["coverage"]["complete"] is False


def test_validator_accepts_finish_after_full_cross_layer_path_coverage() -> None:
    validator = WorkspaceSourceChainCoverageValidator()
    state = _state(
        "apps/web/src/api/tasks.ts",
        "apps/gateway/internal/api/handlers/task.go",
        "apps/agent-worker/src/database/outbox/publisher.py",
        "apps/agent-worker/src/runtime_bus/consumer.py",
        "apps/agent-worker/src/runtime/worker.py",
    )

    result = validator.validate(action=AgentAction.finish("完成"), state=state)

    assert result.accepted is True
    assert result.output == "完成"
    assert result.metadata["coverage"]["complete"] is True


def test_validator_accepts_scoped_uncertainty_after_runtime_coverage_is_complete() -> None:
    validator = WorkspaceSourceChainCoverageValidator()
    state = _state(
        "apps/web/src/api/tasks.ts",
        "apps/agent-worker/src/database/outbox/publisher.py",
        "apps/agent-worker/src/runtime_bus/consumer.py",
        "apps/agent-worker/src/runtime/worker.py",
    )

    result = validator.validate(
        action=AgentAction.finish("Web 到 Gateway 的调用边已核对；重试策略没有直接证据，因此这部分不知道。"),
        state=state,
    )

    assert result.accepted is True
    assert result.metadata["coverage"]["complete"] is True
    assert result.metadata["scoped_uncertainty_count"] == 1


def test_validator_rejects_answer_that_denies_global_chain_coverage() -> None:
    validator = WorkspaceSourceChainCoverageValidator()
    state = _state(
        "apps/web/src/api/tasks.ts",
        "apps/agent-worker/src/database/outbox/publisher.py",
        "apps/agent-worker/src/runtime_bus/consumer.py",
        "apps/agent-worker/src/runtime/worker.py",
    )

    result = validator.validate(
        action=AgentAction.finish("由于证据不足，整条端到端调用链仍然无法确认。"),
        state=state,
    )

    assert result.accepted is False
    assert "整条" in result.feedback
    assert result.reason_code == "SOURCE_CHAIN_GLOBAL_CONTRADICTION"
    assert result.diagnostics["answer_denied_global_coverage"] is True


def test_validator_accepts_explicit_no_gap_statement_after_direct_edge_coverage() -> None:
    validator = WorkspaceSourceChainCoverageValidator()
    state = _state(
        "apps/web/src/api/tasks.ts",
        "apps/agent-worker/src/database/outbox/publisher.py",
        "apps/agent-worker/src/runtime_bus/consumer.py",
        "apps/agent-worker/src/runtime/worker.py",
    )

    result = validator.validate(
        action=AgentAction.finish("已逐边核对，当前没有未确认的调用边。"),
        state=state,
    )

    assert result.accepted is True


def test_validator_ignores_ordinary_single_module_source_question() -> None:
    validator = WorkspaceSourceChainCoverageValidator()
    state = AgentState(
        user_goal="请阅读源码，说明这个函数内部调用链。",
        observations=[_read("src/service.py")],
    )

    result = validator.validate(action=AgentAction.finish("完成"), state=state)

    assert result.accepted is True
