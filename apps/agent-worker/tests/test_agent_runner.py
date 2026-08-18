"""AgentRunner Phase 5 + Phase 6 单元测试。

覆盖：
- AgentAction 构造（finish / call_tool）
- MockModelProvider 决策规则
- AgentRunner call_tool 成功/失败
- AgentRunner cancel
- AgentRunner max_iterations 上限
- MockRunner 委托 AgentRunner（tool scenario）
- MockRunner simple_success / permission scenario 回归
- Phase 6: action validation hardening
  - ModelProvider 返回非 AgentAction（None/dict/str）→ agent.run.failed
  - 未知 action_type → agent.run.failed（不再 completed）
  - call_tool 缺 tool_name / tool_name 非 str / arguments 非 dict → agent.run.failed
  - finish 缺 final_message / final_message 非 str → agent.run.failed
  - 原有成功/失败/cancel 链路不受影响
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.checkpoint import (
    RUN_CHECKPOINT_VERSION,
    is_resumable_run_checkpoint,
)
from jarvis_worker.agent.core.final_answer import FinalAnswerValidation
from jarvis_worker.agent.core.graph_nodes import AgentGraphNodes
from jarvis_worker.agent.core.phases.action_validation import ActionValidationPhase
from jarvis_worker.agent.core.phases.intent_extraction import IntentExtractionPhase
from jarvis_worker.agent.core.phases.lifecycle import RunLifecyclePhase
from jarvis_worker.agent.core.phases.model_call import (
    ModelCallPhase,
    should_buffer_workspace_output,
    should_enter_finish_only,
    should_require_tool_action,
)
from jarvis_worker.agent.core.phases.observation import ObservationPhase
from jarvis_worker.agent.core.phases.tool_execution import ToolExecutionPhase
from jarvis_worker.agent.core.runner import AgentRunner
from jarvis_worker.agent.core.source_chain_validator import (
    WorkspaceSourceChainCoverageValidator,
)
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.intents import (
    IntentEffects,
    IntentExtraction,
    IntentRuntimeContext,
    IntentWorkspace,
    RetrievalIntent,
)
from jarvis_worker.agent.models.errors import ModelProviderError
from jarvis_worker.agent.models.openai_compatible_provider import OpenAiCompatibleModelProvider
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.prompts.builder import PromptBuilder
from jarvis_worker.agent.rag.answer import RagCitationValidator
from jarvis_worker.agent.rag.evidence import (
    RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
    RAG_EVIDENCE_ASSESSMENT_SCHEMA,
)
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest, ToolResult
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.agent.tools.workspace import (
    execute_workspace_list_files,
    execute_workspace_read_file,
)
from jarvis_worker.bootstrap.tool_registry import create_tool_registry
from jarvis_worker.runtime_bus.messages import RunJobMessage
from tests.testing_doubles import MockModelProvider, MockRunner

# ============================================================
# Helpers
# ============================================================


class _FixedActionModelProvider(ModelProvider):
    """返回固定返回值序列的 mock ModelProvider，用于 Phase 6 action validation 测试。

    与 MockModelProvider（规则驱动，总是返回合法 AgentAction）不同，
    本类用于注入非法返回值（None / dict / str / 非法 AgentAction），
    验证 AgentRunner 的边界防御。

    支持：
    - 单个 AgentAction → 每次调用返回相同 action
    - 非 AgentAction 的任意值 → 用于测试 ModelProvider 返回值类型防御
    - AgentAction 列表 → 按顺序返回，超出则返回最后一个
    - 任意值的列表 → 按顺序返回，超出则返回最后一个
    """

    def __init__(self, action: AgentAction | list[AgentAction] | Any):
        if isinstance(action, list):
            self._actions: list = action
        else:
            self._actions = [action]
        self._call_count = 0

    def decide_next_action(self, state: AgentState) -> Any:
        if self._call_count < len(self._actions):
            result = self._actions[self._call_count]
        else:
            result = self._actions[-1]
        self._call_count += 1
        return result


class _StreamingFinishModelProvider(ModelProvider):
    """仅用于验证 AgentRunner 的安全 delta 事件与即时发布。"""

    def __init__(self, message: str):
        self._message = message

    def decide_next_action(self, state: AgentState) -> AgentAction:
        return AgentAction.finish(self._message)

    def decide_next_action_stream(self, state: AgentState, on_text_delta) -> AgentAction:
        for index in range(0, len(self._message), 17):
            on_text_delta(self._message[index : index + 17])
        return AgentAction.finish(self._message)


class _BudgetFeedbackCaptureProvider(ModelProvider):
    """执行一次只读工具后收口，并保存每轮可信 system context。"""

    def __init__(self):
        self.system_messages: list[str] = []

    def decide_next_action(self, state: AgentState) -> AgentAction:
        raise AssertionError("测试必须通过已预算 ContextPackage 调用模型")

    def decide_prepared_context(self, state, context) -> AgentAction:
        self.system_messages.append(context.messages[0].content)
        if state.iteration == 0:
            return AgentAction.call_tool(
                "workspace.list_files",
                {"path": "."},
                "获取当前目录事实",
            )
        return AgentAction.finish("已根据现有目录证据完成收口。")

    def decide_prepared_context_stream(self, state, context, on_text_delta) -> AgentAction:
        del on_text_delta
        return self.decide_prepared_context(state, context)


def _make_job(user_goal: str = "test", workspace_path: str = "") -> RunJobMessage:
    return RunJobMessage(
        job_id="job-1",
        trace_id="trace-1",
        task_id="task-1",
        run_id="run-1",
        user_goal=user_goal,
        created_at="2026-07-09T00:00:00Z",
        workspace_path=workspace_path,
    )


def _make_tool_gateway() -> ToolGateway:
    registry = ToolRegistry()
    registry.register(
        ToolManifest(
            name="workspace.list_files",
            provider="native",
            description="list workspace files",
            risk_level_default="L0",
            permission_scope="workspace",
        ),
        execute_workspace_list_files,
    )
    # Phase 6A: workspace.read_file
    registry.register(
        ToolManifest(
            name="workspace.read_file",
            provider="native",
            description="read workspace text file",
            risk_level_default="L0",
            permission_scope="workspace",
        ),
        execute_workspace_read_file,
    )
    return ToolGateway(registry, PermissionManager())


def _make_rag_tool_gateway() -> ToolGateway:
    registry = ToolRegistry()
    registry.register(
        ToolManifest(
            name="rag.search",
            provider="native",
            description="search workspace RAG documents",
            risk_level_default="L0",
            permission_scope="current_workspace_rag",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1}},
                "required": ["query"],
                "additionalProperties": False,
            },
            allowed_decisions=[],
        ),
        lambda _request: ToolResult(
            ok=True,
            kind="json",
            summary="RAG 检索完成：0 条引用证据",
            data={"results": []},
        ),
    )
    return ToolGateway(registry, PermissionManager())


def _make_agent_runner(gateway: ToolGateway | None = None) -> AgentRunner:
    if gateway is None:
        gateway = _make_tool_gateway()
    model = MockModelProvider()
    return AgentRunner(
        model_provider=model,
        tool_gateway=gateway,
        worker_id="test-agent",
        max_iterations=3,
    )


def _workspace_read_intent(*, retrieval_mode: str = "skip") -> dict[str, Any]:
    return {
        "workspace": {
            "evidence": "required",
            "action": "read",
            "ambiguity": "clear",
        },
        "retrieval": {
            "mode": retrieval_mode,
            "document_scope": "all" if retrieval_mode == "required" else "none",
        },
    }


def test_prior_answer_transform_rejects_tool_before_execution_and_rewrites() -> None:
    model = _FixedActionModelProvider(
        [
            AgentAction.call_tool(
                "rag.search",
                {"query": "新增证据"},
                "重新检索",
            ),
            AgentAction.finish(
                "| 主题 | NIST | NASA | 差异 |\n| --- | --- | --- | --- |\n"
                "| 风险 | 原比较内容 | 原比较内容 | 原差异 |"
            ),
        ]
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_make_rag_tool_gateway(),
        worker_id="test",
        max_iterations=3,
    )

    envelopes = runner.run(_make_job("把上一条比较结果压缩成表格，不要新增没有依据的内容。"))

    event_types = [event.event_type for event in envelopes]
    assert "tool.call.started" not in event_types
    assert "agent.run.completed" in event_types
    failure = next(event for event in envelopes if event.event_type == "model.call.failed")
    assert failure.runtime_event["payload"]["error_code"] == ("HISTORY_TRANSFORM_TOOL_FORBIDDEN")


def _trusted_source_observation(index: int, path: str, content: str) -> dict[str, Any]:
    return {
        "tool_call_id": f"source-{index}",
        "tool_name": "workspace.read_file",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "workspace.read_file",
            "arguments": {"path": path},
            "reason": "读取源码证据",
        },
        "ok": True,
        "summary": f"读取 {path}",
        "data": {
            "path": path,
            "content": content,
            "start_line": 1,
            "end_line": 20,
            "total_lines": 20,
            "truncated": False,
        },
    }


def test_read_only_evidence_task_reserves_last_two_calls_for_finish_only():
    state = AgentState(iteration=12, intent=_workspace_read_intent())
    state.observations.append({"tool_name": "workspace.read_files", "ok": True})

    assert should_enter_finish_only(state, max_iterations=14) is True


def test_read_only_evidence_task_keeps_third_remaining_call_for_navigation():
    state = AgentState(iteration=11, intent=_workspace_read_intent())
    state.observations.append({"tool_name": "workspace.read_files", "ok": True})

    assert should_enter_finish_only(state, max_iterations=14) is False


def test_collection_evidence_task_does_not_enter_finish_only_after_one_file():
    state = AgentState(
        iteration=12,
        user_goal="请阅读相关材料，说明完整流程，并给出每一步的文件依据。",
        intent=_workspace_read_intent(),
    )
    state.observations.append(
        _trusted_source_observation(1, "requests/REQ-42.md", "当前状态：待审批")
    )

    assert should_enter_finish_only(state, max_iterations=14) is False

    state.observations.append(
        {
            "tool_call_id": "list-related-parent",
            "tool_name": "workspace.list_files",
            "model_action": {
                "action_type": "call_tool",
                "tool_name": "workspace.list_files",
                "arguments": {"path": "requests"},
                "reason": "枚举相关父目录",
            },
            "ok": True,
            "summary": "枚举 requests",
            "data": {
                "entries": [
                    {
                        "name": "REQ-42.md",
                        "path": "/trusted/workspace/requests/REQ-42.md",
                        "type": "file",
                    }
                ]
            },
        }
    )
    state.observations.append(
        _trusted_source_observation(2, "procedure.md", "1. 提交\n2. 审批\n3. 执行")
    )
    assert should_enter_finish_only(state, max_iterations=14) is True


def test_answer_validation_retry_forces_finish_only_independent_of_tool_budget():
    state = AgentState(
        iteration=1,
        answer_guard_rejections=1,
        answer_guard_feedback="只重写最终回答",
    )

    assert should_enter_finish_only(state, max_iterations=14) is True


def test_source_evidence_retry_keeps_tool_planning_available() -> None:
    state = AgentState(
        iteration=12,
        user_goal="请阅读源码，说明从 Web 到 Worker 的端到端执行路径。",
        intent=_workspace_read_intent(),
        source_chain_evidence_rejections=1,
        effect_guard_feedback="补齐任一未覆盖证据面",
    )
    state.observations.append(
        _trusted_source_observation(
            1, "apps/web/src/api/tasks.ts", "return apiPost('/tasks', input)"
        )
    )

    assert should_enter_finish_only(state, max_iterations=14) is False
    assert should_require_tool_action(state, max_iterations=14) is True


def test_effect_guard_retry_enters_tool_required_mode_until_new_tool_result() -> None:
    state = AgentState(
        iteration=2,
        effect_guard_rejections=1,
        effect_guard_feedback="补齐 Workspace 多材料正文覆盖",
    )

    assert should_require_tool_action(state, max_iterations=14) is True

    # ObservationPhase 在一次真实 ToolResult 后消费反馈；历史拒绝计数仍可保留用于审计，
    # 但不得把后续普通规划永久锁在 tool-required 模式。
    state.effect_guard_feedback = ""
    assert should_require_tool_action(state, max_iterations=14) is False


def test_model_output_retry_feedback_alone_does_not_force_tool_action() -> None:
    state = AgentState(
        iteration=1,
        model_output_rejections=1,
        effect_guard_feedback="上一次 AgentAction JSON 结构无效",
    )

    assert should_require_tool_action(state, max_iterations=14) is False


def test_source_evidence_retry_does_not_require_tool_after_budget_exhaustion() -> None:
    state = AgentState(
        iteration=14,
        source_chain_evidence_rejections=1,
        effect_guard_feedback="补齐任一未覆盖证据面",
    )

    assert should_require_tool_action(state, max_iterations=14) is False


def test_answer_rewrite_takes_priority_over_tool_required_mode() -> None:
    state = AgentState(
        iteration=3,
        source_chain_evidence_rejections=1,
        answer_guard_rejections=1,
        answer_guard_feedback="只重写最终回答",
    )

    assert should_require_tool_action(state, max_iterations=14) is False


def test_finish_only_waits_for_required_rag_evidence():
    state = AgentState(iteration=12, intent=_workspace_read_intent(retrieval_mode="required"))
    state.observations.append({"tool_name": "workspace.read_files", "ok": True})

    assert should_enter_finish_only(state, max_iterations=14) is False

    state.observations.append({"tool_name": "rag.search", "ok": True})
    assert should_enter_finish_only(state, max_iterations=14) is True


def test_finish_only_waits_for_cross_layer_source_endpoint_coverage():
    state = AgentState(
        iteration=12,
        user_goal=("请阅读这个代码库，说明 Web 创建任务后直到 Worker 开始执行的真实调用链。"),
        intent=_workspace_read_intent(),
    )
    state.observations.append(
        {
            "tool_name": "workspace.read_file",
            "ok": True,
            "data": {
                "path": "apps/agent-worker/src/runtime/worker.py",
                "content": "self._process_job_with_cancel_check(job)",
            },
        }
    )

    assert should_enter_finish_only(state, max_iterations=14) is False

    state.observations.extend(
        [
            {
                "tool_name": "workspace.read_file",
                "ok": True,
                "data": {
                    "path": "apps/web/src/api/tasks.ts",
                    "content": 'return apiPost<CreateTaskOutput>("/tasks", input)',
                },
            },
            {
                "tool_name": "workspace.read_file",
                "ok": True,
                "data": {
                    "path": "apps/gateway/internal/api/handlers/task.go",
                    "content": "response = h.controlPlane.CreateTask(ctx, request)",
                },
            },
            {
                "tool_name": "workspace.read_file",
                "ok": True,
                "data": {
                    "path": "apps/agent-worker/src/database/outbox/publisher.py",
                    "content": 'EVENT_TO_STREAM = {"task.created": "run-queue"}',
                },
            },
            {
                "tool_name": "workspace.read_file",
                "ok": True,
                "data": {
                    "path": "apps/agent-worker/src/runtime_bus/consumer.py",
                    "content": 'client.xreadgroup(streams={RUN_QUEUE: ">"})',
                },
            },
        ]
    )
    assert should_enter_finish_only(state, max_iterations=14) is True


def test_workspace_evidence_output_is_buffered_until_agent_action_is_valid():
    assert should_buffer_workspace_output(AgentState(intent=_workspace_read_intent())) is True
    assert should_buffer_workspace_output(AgentState(intent=None)) is False


# ============================================================
# AgentAction
# ============================================================


class TestAgentAction:
    """AgentAction 构造测试。"""

    def test_finish_action(self):
        action = AgentAction.finish("任务完成")
        assert action.action_type == "finish"
        assert action.final_message == "任务完成"

    def test_call_tool_action(self):
        action = AgentAction.call_tool(
            tool_name="workspace.list_files",
            arguments={"workspace_root": "/tmp", "path": "."},
            reason="需要列出文件",
        )
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.list_files"
        assert action.arguments["workspace_root"] == "/tmp"
        assert action.reason == "需要列出文件"


# ============================================================
# AgentState
# ============================================================


class TestAgentState:
    """AgentState 测试。"""

    def test_initial_state(self):
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="列出文件",
            workspace_root="/tmp",
        )
        assert state.task_id == "t1"
        assert state.iteration == 0
        assert state.observations == []

    def test_runner_compiles_langgraph_control_flow(self):
        """图装配与节点 owner 分离，且仍拥有既有控制流。"""
        runner = _make_agent_runner()
        assert isinstance(runner._graph_nodes, AgentGraphNodes)
        assert isinstance(runner._action_validation_phase, ActionValidationPhase)
        assert isinstance(runner._intent_extraction_phase, IntentExtractionPhase)
        assert isinstance(runner._lifecycle_phase, RunLifecyclePhase)
        assert isinstance(runner._model_call_phase, ModelCallPhase)
        assert isinstance(runner._observation_phase, ObservationPhase)
        assert isinstance(runner._tool_execution_phase, ToolExecutionPhase)
        assert runner._graph_nodes._call_model == runner._model_call_phase.run
        assert runner._graph_nodes._extract_intent == runner._intent_extraction_phase.run
        assert runner._graph_nodes._initialize_run == runner._lifecycle_phase.initialize_run
        assert (
            runner._graph_nodes._build_max_iterations_failure
            == runner._lifecycle_phase.build_max_iterations_failure
        )
        assert runner._graph_nodes._validate_action == runner._action_validation_phase.run
        assert runner._graph_nodes._execute_tool == runner._tool_execution_phase.run
        assert runner._graph_nodes._observe_result == runner._observation_phase.run
        graph = runner._graph.get_graph()
        assert {
            "__start__",
            "initialize_run",
            "call_model",
            "validate_action",
            "execute_tool",
            "observe_result",
            "max_iterations",
            "__end__",
        } <= set(graph.nodes)

    def test_add_observation(self):
        state = AgentState(task_id="t1", run_id="r1")
        state.add_observation({"ok": True, "summary": "success"})
        assert state.iteration == 1
        assert len(state.observations) == 1
        assert state.observations[0]["ok"] is True


# ============================================================
# MockModelProvider
# ============================================================


class TestMockModelProvider:
    """MockModelProvider 决策规则测试。"""

    def test_tool_keyword_call_tool(self):
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="列出当前 workspace 文件",
            workspace_root="/tmp",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.list_files"
        # workspace_root 由 AgentRunner 注入，MockModelProvider 不提供
        assert "workspace_root" not in action.arguments
        assert action.arguments["path"] == "."

    def test_tool_keyword_english(self):
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="list files in workspace",
            workspace_root="/tmp",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"

    def test_no_workspace_root_still_call_tool(self):
        """空 workspace_root 仍然返回 call_tool（fail closed 由 ToolGateway 保证）。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="列出文件",
            workspace_root="",  # 空——ToolGateway 会返回 WORKSPACE_ROOT_REQUIRED
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.list_files"
        # workspace_root 由 AgentRunner 注入，MockModelProvider 不提供
        # 空 workspace_root 由 AgentRunner 从 state 注入后交给 ToolGateway fail closed
        assert "workspace_root" not in action.arguments

    def test_normal_goal_finish(self):
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="hello world",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "finish"

    def test_second_iteration_after_success(self):
        """工具执行成功后，下一轮返回 finish。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="列出文件",
            workspace_root="/tmp",
            iteration=1,
        )
        state.add_observation({"ok": True, "summary": "列出了 5 个文件"})
        # 重新设置 iteration（add_observation 会递增，但这里模拟第二轮）
        action = model.decide_next_action(state)
        assert action.action_type == "finish"
        assert "成功" in action.final_message

    def test_second_iteration_after_failure_defensive(self):
        """工具失败已在 AgentRunner 层 terminate；防御性 fallback 返回 finish。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="列出文件",
            workspace_root="/tmp",
            iteration=1,
        )
        state.add_observation(
            {
                "ok": False,
                "error": {"code": "PERMISSION_DENIED", "message": "权限拒绝"},
            }
        )
        action = model.decide_next_action(state)
        assert action.action_type == "finish"

    # -- Phase 6A: read_file 触发 --

    def test_read_file_chinese_trigger(self):
        """用户输入 "读取 AGENTS.md" → call_tool workspace.read_file。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="读取 AGENTS.md",
            workspace_root="/tmp",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.read_file"
        assert action.arguments["path"] == "AGENTS.md"
        # workspace_root 由 AgentRunner 注入，MockModelProvider 不提供
        assert "workspace_root" not in action.arguments

    def test_read_file_english_trigger(self):
        """用户输入 "read CLAUDE.md" → call_tool workspace.read_file。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="read CLAUDE.md",
            workspace_root="/tmp",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.read_file"
        assert action.arguments["path"] == "CLAUDE.md"

    def test_read_file_with_path_prefix(self):
        """用户输入 "查看 docs/README.md 内容" → call_tool workspace.read_file。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="查看 docs/README.md 内容",
            workspace_root="/tmp",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.read_file"
        assert action.arguments["path"] == "docs/README.md"

    def test_read_file_summarize_trigger(self):
        """用户输入 "总结 AGENTS.md" → call_tool workspace.read_file。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="总结 AGENTS.md",
            workspace_root="/project",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.read_file"
        assert action.arguments["path"] == "AGENTS.md"

    def test_read_file_no_filename_returns_finish(self):
        """用户输入 "总结文档" 但没有文件名 → finish（不猜文件）。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="总结文档内容",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "finish"

    def test_list_files_still_has_priority_over_read_when_no_filename(self):
        """list_files 关键词（无文件名）→ 仍触发 list_files。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="查看文件",
            workspace_root="/tmp",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.list_files"

    # -- Phase 6A 补测：查看 / view / 打开 关键词 --

    def test_read_file_view_chinese_content_trigger(self):
        """ "查看 AGENTS.md 内容" → call_tool workspace.read_file。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="查看 AGENTS.md 内容",
            workspace_root="/tmp",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.read_file"
        assert action.arguments["path"] == "AGENTS.md"

    def test_read_file_view_english_trigger(self):
        """ "view AGENTS.md" → call_tool workspace.read_file。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="view AGENTS.md",
            workspace_root="/tmp",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.read_file"

    def test_read_file_open_trigger(self):
        """ "打开 AGENTS.md" → call_tool workspace.read_file。"""
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="打开 AGENTS.md",
            workspace_root="/project",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.read_file"
        assert action.arguments["path"] == "AGENTS.md"


# ============================================================
# AgentRunner
# ============================================================


class TestAgentRunner:
    """AgentRunner 最小循环测试。"""

    def test_tool_success_flow(self):
        """call_tool 成功：run.started → tool.started → tool.finished → run.completed。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "readme.md").write_text("hello")

            runner = _make_agent_runner()
            job = _make_job("列出文件", workspace_path=tmpdir)
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types
            assert "agent.run.completed" in event_types

            # 验证 finished event 中的结果
            finished = next(e for e in envelopes if e.event_type == "tool.call.finished")
            tc = finished.runtime_event["payload"]["tool_call"]
            assert tc["status"] == "completed"
            assert tc["result"] is not None

            # 验证最终输出
            completed = next(e for e in envelopes if e.event_type == "agent.run.completed")
            assert "成功" in completed.runtime_event["payload"]["output"]

    def test_tool_no_workspace_root_fail_closed(self):
        """无 workspace_root 时 ToolGateway fail closed → tool.call.failed + agent.run.failed。"""
        runner = _make_agent_runner()
        job = _make_job(
            "列出文件", workspace_path=""
        )  # 无 workspace_path + 无 default_workspace_root
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "agent.run.started" in event_types
        assert "tool.call.started" in event_types  # 仍然发起工具调用
        assert "tool.call.failed" in event_types  # fail closed
        assert "agent.run.failed" in event_types  # terminal failure
        assert "agent.run.completed" not in event_types  # 不应 completed

        # 验证 tool.call.failed 错误码
        failed = next(e for e in envelopes if e.event_type == "tool.call.failed")
        tc = failed.runtime_event["payload"]["tool_call"]
        assert tc["status"] == "failed"
        assert tc["error"]["code"] == "WORKSPACE_ROOT_REQUIRED"

        # 验证 agent.run.failed 使用 AppError shape
        run_failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        err = run_failed.runtime_event["payload"]["error"]
        assert err["code"] == "WORKSPACE_ROOT_REQUIRED"
        assert err["category"] == "permission"

    def test_normal_goal_finish(self):
        """普通目标直接 finish。"""
        runner = _make_agent_runner()
        job = _make_job("hello world")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "agent.run.started" in event_types
        assert "agent.run.completed" in event_types
        assert "tool.call.started" not in event_types

    def test_streaming_final_message_is_published_immediately_with_bounded_deltas(self):
        message = "流式输出" * 80
        runner = AgentRunner(
            model_provider=_StreamingFinishModelProvider(message),
            tool_gateway=_make_tool_gateway(),
            worker_id="test-agent",
        )
        published: list = []

        envelopes = runner.run(_make_job("hello world"), publish_cb=published.append)

        delta_events = [e for e in envelopes if e.event_type == "model.delta"]
        assert delta_events
        assert "".join(e.runtime_event["payload"]["delta"] for e in delta_events) == message
        assert all("accumulated" not in e.runtime_event["payload"] for e in delta_events)
        assert all(len(e.runtime_event["payload"]["delta"]) <= 128 for e in delta_events)
        assert [e.event_type for e in published][:2] == [
            "agent.run.started",
            "model.call.started",
        ]
        assert any(e.event_type == "model.delta" for e in published)

    def test_tool_failure_flow(self):
        """L2 工具被用户拒绝 → 合法 failed 状态并终止 run。"""
        # 用 L2 risk，模拟用户明确拒绝
        registry = ToolRegistry()
        registry.register(
            ToolManifest(
                name="workspace.list_files",
                risk_level_default="L2",
            ),
            execute_workspace_list_files,
        )
        gateway = ToolGateway(registry)

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = _make_agent_runner(gateway)
            job = _make_job("list files", workspace_path=tmpdir)
            envelopes = runner.run(job, wait_decision=lambda _request_id: "deny")

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "tool.call.started" in event_types
            assert "permission.required" in event_types
            assert "permission.resolved" in event_types
            assert "tool.call.failed" in event_types
            assert "agent.run.failed" in event_types
            assert "agent.run.completed" not in event_types

            failed = next(e for e in envelopes if e.event_type == "tool.call.failed")
            tc = failed.runtime_event["payload"]["tool_call"]
            assert tc["status"] == "failed"
            assert tc["permission_status"] == "denied"
            assert tc.get("error", {}).get("code") == "PERMISSION_DENIED"

    def test_l2_tool_approval_uses_real_manifest_and_resumes(self):
        registry = ToolRegistry()
        registry.register(
            ToolManifest(
                name="workspace.list_files",
                provider="native",
                risk_level_default="L2",
                permission_scope="workspace",
                allowed_decisions=["allow_once", "deny"],
            ),
            execute_workspace_list_files,
        )
        runner = _make_agent_runner(ToolGateway(registry, PermissionManager()))
        prepared: list[str] = []
        published: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            envelopes = runner.run(
                _make_job("list files", workspace_path=tmpdir),
                prepare_wait=prepared.append,
                wait_decision=lambda _request_id: "allow_once",
                publish_cb=lambda envelope: published.append(envelope.event_id),
            )

        event_types = [event.event_type for event in envelopes]
        assert "permission.required" in event_types
        assert "permission.resolved" in event_types
        assert "tool.call.finished" in event_types
        assert event_types[-1] == "agent.run.completed"
        request = next(
            event for event in envelopes if event.event_type == "permission.required"
        ).runtime_event["payload"]["request"]
        assert prepared == [request["id"]]
        assert request["risk_level"] == "L2"
        assert request["allowed_decisions"] == ["allow_once", "deny"]
        finished = next(
            event for event in envelopes if event.event_type == "tool.call.finished"
        ).runtime_event["payload"]["tool_call"]
        assert finished["risk_level"] == "L2"
        assert finished["permission_status"] == "approved"
        assert finished["permission_request_id"] == request["id"]
        assert published

    def test_l2_tool_timeout_uses_expired_permission_status(self):
        registry = ToolRegistry()
        registry.register(
            ToolManifest(name="workspace.list_files", risk_level_default="L2"),
            execute_workspace_list_files,
        )
        model = _FixedActionModelProvider(
            AgentAction.call_tool("workspace.list_files", {"path": "."}, "查看文件")
        )
        runner = AgentRunner(model, ToolGateway(registry, PermissionManager()))

        with tempfile.TemporaryDirectory() as tmpdir:
            events = runner.run(
                _make_job("list files", workspace_path=tmpdir),
                wait_decision=lambda _request_id: None,
            )

        failed = next(event for event in events if event.event_type == "tool.call.failed")
        tool_call = failed.runtime_event["payload"]["tool_call"]
        assert tool_call["permission_status"] == "expired"
        assert tool_call["error"]["code"] == "PERMISSION_TIMEOUT"

    def test_l2_tool_deferred_checkpoint_resumes_without_duplicate_start(self):
        registry = ToolRegistry()
        published = []

        def execute_after_persisted_effect_boundary(request):
            assert published
            assert published[-1].event_type == "permission.resolved"
            assert published[-1].internal["run_checkpoint"]["resume_node"] == "tool_in_flight"
            return execute_workspace_list_files(request)

        registry.register(
            ToolManifest(
                name="workspace.list_files",
                provider="native",
                risk_level_default="L2",
                allowed_decisions=["allow_once", "deny"],
            ),
            execute_after_persisted_effect_boundary,
        )
        model = _FixedActionModelProvider(
            [
                AgentAction.call_tool(
                    "workspace.list_files",
                    {"path": ".", "token": "secret-value"},
                    "查看文件",
                ),
                AgentAction.finish("已完成"),
            ]
        )
        runner = AgentRunner(model, ToolGateway(registry, PermissionManager()))

        with tempfile.TemporaryDirectory() as tmpdir:
            first = runner.run(
                _make_job("list files", workspace_path=tmpdir),
                defer_permission=True,
            )
            assert [event.event_type for event in first][-1] == "permission.required"
            request = first[-1].runtime_event["payload"]["request"]
            checkpoint = request["_internal_checkpoint"]
            assert request["arguments_summary"]["token"] == "***"
            assert checkpoint["tool_request"]["arguments"]["token"] == "secret-value"

            resumed = runner.resume_permission(
                checkpoint, "allow_once", publish_cb=published.append
            )

        resumed_types = [event.event_type for event in resumed]
        assert resumed_types == [
            "permission.resolved",
            "tool.call.finished",
            "model.call.started",
            "model.context.prepared",
            "model.call.completed",
            "artifact.created",
            "agent.run.completed",
        ]
        assert "agent.run.started" not in resumed_types
        assert resumed[1].runtime_event["payload"]["tool_call"]["permission_status"] == "approved"
        assert [event.event_type for event in published] == ["permission.resolved"]

    def test_l2_tool_deferred_deny_uses_failed_contract(self):
        registry = ToolRegistry()
        registry.register(
            ToolManifest(name="workspace.list_files", risk_level_default="L2"),
            execute_workspace_list_files,
        )
        model = _FixedActionModelProvider(
            AgentAction.call_tool("workspace.list_files", {"path": "."}, "查看文件")
        )
        runner = AgentRunner(model, ToolGateway(registry, PermissionManager()))

        with tempfile.TemporaryDirectory() as tmpdir:
            first = runner.run(
                _make_job("list files", workspace_path=tmpdir),
                defer_permission=True,
            )
            checkpoint = first[-1].runtime_event["payload"]["request"]["_internal_checkpoint"]
            resumed = runner.resume_permission(checkpoint, "deny")

        assert [event.event_type for event in resumed] == [
            "permission.resolved",
            "tool.call.failed",
            "agent.run.failed",
        ]
        tool_call = resumed[1].runtime_event["payload"]["tool_call"]
        assert tool_call["status"] == "failed"
        assert tool_call["permission_status"] == "denied"
        assert tool_call["error"]["code"] == "PERMISSION_DENIED"

    def test_l2_approved_execution_failure_is_not_mislabeled_as_denied(self):
        from jarvis_worker.bootstrap.tool_registry import create_tool_registry

        model = _FixedActionModelProvider(
            AgentAction.call_tool(
                "workspace.create_file",
                {"path": "exists.txt", "content": "new content"},
                "创建文件",
            )
        )
        runner = AgentRunner(
            model,
            ToolGateway(create_tool_registry(), PermissionManager()),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "exists.txt").write_text("old content")
            first = runner.run(
                _make_job("create file", workspace_path=tmpdir),
                defer_permission=True,
            )
            request = first[-1].runtime_event["payload"]["request"]
            content_summary = request["arguments_summary"]["content"]
            assert content_summary["redacted"] is True
            assert content_summary["size_bytes"] == len("new content".encode())
            public_request = {
                key: value for key, value in request.items() if key != "_internal_checkpoint"
            }
            assert "new content" not in json.dumps(public_request, ensure_ascii=False)

            resumed = runner.resume_permission(
                request["_internal_checkpoint"],
                "allow_once",
            )

        event_types = [event.event_type for event in resumed]
        assert event_types[:2] == ["permission.resolved", "tool.call.failed"]
        assert "model.call.started" in event_types
        assert "agent.run.failed" in event_types
        assert "permission.required" not in event_types
        tool_call = resumed[1].runtime_event["payload"]["tool_call"]
        assert tool_call["permission_status"] == "approved"
        assert tool_call["status"] == "failed"
        assert tool_call["error"]["code"] == "FILE_ALREADY_EXISTS"
        terminal = next(event for event in resumed if event.event_type == "agent.run.failed")
        assert terminal.runtime_event["payload"]["error"]["code"] == ("FILE_ALREADY_EXISTS")

    def test_permission_resume_accepts_retired_skill_checkpoint_field(self):
        registry = ToolRegistry()
        registry.register(
            ToolManifest(name="workspace.list_files", risk_level_default="L2"),
            execute_workspace_list_files,
        )
        model = _FixedActionModelProvider(
            [
                AgentAction.call_tool("workspace.list_files", {"path": "."}, "查看文件"),
                AgentAction.finish("操作已完成。"),
            ]
        )
        runner = AgentRunner(model, ToolGateway(registry, PermissionManager()))

        with tempfile.TemporaryDirectory() as tmpdir:
            first = runner.run(
                _make_job("执行一次目录查看", workspace_path=tmpdir),
                defer_permission=True,
            )
            checkpoint = first[-1].runtime_event["payload"]["request"]["_internal_checkpoint"]
            checkpoint["state"]["skill_workflow_stage"] = ""
            resumed = runner.resume_permission(checkpoint, "allow_once")

        assert resumed[0].event_type == "permission.resolved"
        assert "tool.call.finished" in [event.event_type for event in resumed]
        assert resumed[-1].event_type == "agent.run.completed"

    def test_permission_resume_preserves_artifact_ids_for_next_model_turn(self):
        artifact_id = str(uuid4())
        seen_observations = []

        class CapturingProvider(ModelProvider):
            def decide_next_action(self, state):
                seen_observations.append(list(state.observations))
                if not state.observations:
                    return AgentAction.call_tool("test.create_artifact", {}, "生成产物")
                return AgentAction.finish("产物已创建。")

        registry = ToolRegistry()
        registry.register(
            ToolManifest(name="test.create_artifact", risk_level_default="L2"),
            lambda _request: ToolResult(
                ok=True,
                kind="artifact",
                summary="产物已创建",
                data={"status": "created"},
                artifact_ids=[artifact_id],
            ),
        )
        runner = AgentRunner(CapturingProvider(), ToolGateway(registry, PermissionManager()))
        first = runner.run(_make_job("执行一次产物生成"), defer_permission=True)
        checkpoint = first[-1].runtime_event["payload"]["request"]["_internal_checkpoint"]

        resumed = runner.resume_permission(checkpoint, "allow_once")

        assert resumed[-1].event_type == "agent.run.completed"
        assert seen_observations[-1][0]["artifact_ids"] == [artifact_id]

    def test_cancel_before_loop(self):
        """取消检查在循环开始时触发。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = _make_agent_runner()
            job = _make_job("列出文件", workspace_path=tmpdir)

            # 第一次 cancel_check 就返回 True
            envelopes = runner.run(job, cancel_check=lambda: True)

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "agent.run.cancelled" in event_types
            assert "tool.call.started" not in event_types
            assert "agent.run.completed" not in event_types

            # verified produced_by 使用实例 worker_id
            cancelled = next(e for e in envelopes if e.event_type == "agent.run.cancelled")
            assert cancelled.produced_by == "test-agent"

    def test_cancel_during_loop(self):
        """在工具执行过程中取消。"""
        call_count = [0]

        def cancel_check():
            call_count[0] += 1
            # 第 2 次调用返回 True（在 tool.call.started 之后）
            return call_count[0] > 2

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = _make_agent_runner()
            job = _make_job("list files", workspace_path=tmpdir)
            envelopes = runner.run(job, cancel_check=cancel_check)

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "agent.run.cancelled" in event_types

    def test_max_iterations(self):
        """工具预算耗尽后仍允许模型基于最后一次观测收口。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            gateway = _make_tool_gateway()
            model = MockModelProvider()
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=gateway,
                worker_id="test",
                max_iterations=1,  # 只允许 1 轮
            )
            job = _make_job("列出文件", workspace_path=tmpdir)
            envelopes = runner.run(job)

            # 有 tool 事件
            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types

            assert "agent.run.completed" in event_types
            assert "agent.run.failed" not in event_types

    def test_tool_budget_is_injected_and_last_model_turn_is_explicit_finish_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model = _BudgetFeedbackCaptureProvider()
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
                max_iterations=1,
            )

            envelopes = runner.run(_make_job("列出文件", workspace_path=tmpdir))

        assert len(model.system_messages) == 2
        assert "已使用 0/1，剩余 1 次" in model.system_messages[0]
        assert "工具调用预算已耗尽（已使用 1/1）" in model.system_messages[1]
        assert "不得请求任何新动作" in model.system_messages[1]
        assert "终态收口模式" in model.system_messages[1]
        assert "当前允许的工具列表：" not in model.system_messages[1]
        assert "workspace.list_files" not in model.system_messages[1]
        assert any(event.event_type == "agent.run.completed" for event in envelopes)

    def test_source_read_activates_chain_feedback_and_ledger_without_source_only_search(self):
        class _SourceEvidenceCaptureProvider(ModelProvider):
            def __init__(self):
                self.contexts = []

            def decide_next_action(self, state):
                raise AssertionError("测试必须通过已预算 ContextPackage 调用模型")

            def decide_prepared_context(self, state, context):
                self.contexts.append(context)
                if state.iteration == 0:
                    return AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "worker.py", "start_line": 1, "max_lines": 50},
                        "读取执行入口",
                    )
                return AgentAction.finish("已根据源码证据完成。")

            def decide_prepared_context_stream(self, state, context, on_text_delta):
                del on_text_delta
                return self.decide_prepared_context(state, context)

        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "worker.py").write_text(
                "def run_forever():\n    dispatch_to_executor()\n",
                encoding="utf-8",
            )
            model = _SourceEvidenceCaptureProvider()
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                final_answer_validators=(WorkspaceSourceChainCoverageValidator(),),
                worker_id="test",
            )

            envelopes = runner.run(_make_job("解释源码执行链", workspace_path=tmpdir))

        assert len(model.contexts) == 2
        second = model.contexts[1]
        assert "源码证据覆盖" in second.messages[0].content
        ledger = next(
            message.content
            for message in second.messages
            if "Runtime 源码证据账本" in message.content
        )
        assert '"path": "worker.py"' in ledger
        assert "dispatch_to_executor" in ledger
        assert any(event.event_type == "agent.run.completed" for event in envelopes)

    def test_source_chain_validator_reopens_planning_after_premature_half_chain_finish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "worker.py").write_text(
                "def run_worker():\n"
                "    client.xreadgroup(streams={RUN_QUEUE: '>'})\n"
                "    self._process_job_with_cancel_check(job)\n",
                encoding="utf-8",
            )
            gateway_path = Path(tmpdir, "apps", "web", "gateway.py")
            gateway_path.parent.mkdir(parents=True)
            gateway_path.write_text(
                "def create_task():\n"
                "    return apiPost('/tasks', input)\n"
                "EVENT_TO_STREAM = {'task.created': 'run-queue'}\n",
                encoding="utf-8",
            )
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "apps/web/gateway.py"},
                        "读取入口和生产端",
                    ),
                    AgentAction.finish("只确认了 Web 半链。"),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "worker.py"},
                        "补 Worker 和消费端",
                    ),
                    AgentAction.finish("入口、传输和 Worker 终点均有源码依据。"),
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                final_answer_validators=(WorkspaceSourceChainCoverageValidator(),),
                worker_id="test",
                max_iterations=4,
            )

            envelopes = runner.run(
                _make_job(
                    "请阅读这个代码库，说明 Web 到 Worker 的真实调用链和文件依据。",
                    workspace_path=tmpdir,
                )
            )

        event_types = [event.event_type for event in envelopes]
        assert event_types.count("tool.call.finished") == 2
        assert "model.call.failed" in event_types
        assert "agent.run.completed" in event_types
        assert "agent.run.failed" not in event_types
        validation = next(
            event.runtime_event["payload"]["validation"]
            for event in envelopes
            if event.event_type == "model.call.failed"
        )
        assert validation["reason_code"] == "SOURCE_CHAIN_EVIDENCE_INCOMPLETE"
        assert validation["recovery_mode"] == "tool_planning"
        assert validation["rewrite_available"] is False
        recovery_failure = next(
            event for event in envelopes if event.event_type == "model.call.failed"
        )
        checkpoint_state = recovery_failure.internal["run_checkpoint"]["state"]
        assert checkpoint_state["source_chain_evidence_rejections"] == 1
        assert checkpoint_state["answer_guard_rejections"] == 0
        assert checkpoint_state["answer_guard_feedback"] == ""
        assert model._call_count == 4

    def test_tool_required_mode_rejects_repeated_finish_then_recovers_with_real_tool_result(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "worker.py").write_text(
                "def run_worker():\n"
                "    client.xreadgroup(streams={RUN_QUEUE: '>'})\n"
                "    self._process_job_with_cancel_check(job)\n",
                encoding="utf-8",
            )
            gateway_path = Path(tmpdir, "apps", "web", "gateway.py")
            gateway_path.parent.mkdir(parents=True)
            gateway_path.write_text(
                "def create_task():\n"
                "    return apiPost('/tasks', input)\n"
                "EVENT_TO_STREAM = {'task.created': 'run-queue'}\n",
                encoding="utf-8",
            )
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "apps/web/gateway.py"},
                        "读取入口和生产端",
                    ),
                    AgentAction.finish("只确认了 Web 半链。"),
                    AgentAction.finish("仍然尝试提前结束。"),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "worker.py"},
                        "补 Worker 和消费端",
                    ),
                    AgentAction.finish("入口、传输和 Worker 终点均有源码依据。"),
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                final_answer_validators=(WorkspaceSourceChainCoverageValidator(),),
                worker_id="test",
                max_iterations=4,
            )

            envelopes = runner.run(
                _make_job(
                    "请阅读这个代码库，说明 Web 到 Worker 的真实调用链和文件依据。",
                    workspace_path=tmpdir,
                )
            )

        event_types = [event.event_type for event in envelopes]
        assert event_types.count("tool.call.finished") == 2
        assert event_types.count("model.call.failed") == 2
        assert "agent.run.completed" in event_types
        assert "agent.run.failed" not in event_types
        failure_codes = [
            event.runtime_event["payload"]["error_code"]
            for event in envelopes
            if event.event_type == "model.call.failed"
        ]
        assert failure_codes == [
            "SOURCE_CHAIN_EVIDENCE_INCOMPLETE",
            "MODEL_OUTPUT_INVALID",
        ]
        output_failure = next(
            event
            for event in envelopes
            if event.event_type == "model.call.failed"
            and event.runtime_event["payload"]["error_code"] == "MODEL_OUTPUT_INVALID"
        )
        assert output_failure.runtime_event["payload"]["output_failure_kind"] == (
            "unsupported_action"
        )
        action_modes = [
            event.runtime_event["payload"]["action_mode"]
            for event in envelopes
            if event.event_type == "model.context.prepared"
        ]
        assert action_modes == [
            "normal",
            "normal",
            "tool_required",
            "tool_required",
            "normal",
        ]
        tool_finished = [event for event in envelopes if event.event_type == "tool.call.finished"][
            -1
        ]
        assert (
            tool_finished.internal["run_checkpoint"]["state"]["source_chain_evidence_rejections"]
            == 0
        )
        assert model._call_count == 5

    def test_source_chain_incomplete_at_tool_limit_fails_once_without_answer_rewrite(self):
        state = AgentState(
            task_id="task-1",
            run_id="run-1",
            user_goal="请阅读源码，说明从 Web 到 Worker 的端到端执行路径。",
            observations=[
                _trusted_source_observation(
                    1,
                    "apps/web/src/api/tasks.ts",
                    "return apiPost('/tasks', input)",
                )
            ],
            iteration=1,
        )
        model = _FixedActionModelProvider(AgentAction.finish("只确认了 Web 入口。"))
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            final_answer_validators=(WorkspaceSourceChainCoverageValidator(),),
            worker_id="test",
            max_iterations=1,
        )

        envelopes = runner.run(
            _make_job(state.user_goal),
            _initial_state=state,
            _emit_run_started=False,
        )

        failures = [event for event in envelopes if event.event_type == "model.call.failed"]
        assert len(failures) == 1
        assert failures[0].runtime_event["payload"]["error_code"] == (
            "SOURCE_CHAIN_EVIDENCE_INCOMPLETE"
        )
        assert failures[0].runtime_event["payload"]["recoverable"] is False
        assert failures[0].runtime_event["payload"]["validation"]["recovery_mode"] == ("none")
        terminal = next(
            event.runtime_event["payload"]["error"]
            for event in envelopes
            if event.event_type == "agent.run.failed"
        )
        assert terminal["code"] == "SOURCE_CHAIN_EVIDENCE_INCOMPLETE"
        assert model._call_count == 1

    def test_final_answer_rewrite_has_independent_bounded_budget_at_tool_limit(self):
        observations = [
            _trusted_source_observation(
                1, "apps/web/src/api/tasks.ts", "return apiPost('/tasks', input)"
            ),
            _trusted_source_observation(
                2,
                "runtime/outbox/publisher.py",
                "EVENT_TO_STREAM = {'task.created': 'run-queue'}",
            ),
            _trusted_source_observation(
                3,
                "runtime/consumer.py",
                "client.xreadgroup(streams={RUN_QUEUE: '>'})",
            ),
            _trusted_source_observation(
                4,
                "runtime/worker.py",
                "self._process_job_with_cancel_check(job)",
            ),
        ]
        state = AgentState(
            task_id="task-1",
            run_id="run-1",
            user_goal="请阅读这个代码库，说明 Web 到 Worker 的真实调用链和文件依据。",
            observations=observations,
            iteration=1,
        )
        model = _FixedActionModelProvider(
            [
                AgentAction.finish("由于证据不足，整条端到端调用链仍然无法确认。"),
                AgentAction.finish(
                    "必需调用链已逐边核对；重试策略没有直接证据，因此这部分不知道。"
                ),
            ]
        )
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            final_answer_validators=(WorkspaceSourceChainCoverageValidator(),),
            worker_id="test",
            max_iterations=1,
        )

        envelopes = runner.run(
            _make_job(state.user_goal),
            _initial_state=state,
            _emit_run_started=False,
        )

        failures = [event for event in envelopes if event.event_type == "model.call.failed"]
        assert len(failures) == 1
        validation = failures[0].runtime_event["payload"]["validation"]
        assert validation == {
            "validator_id": "workspace-source-chain-coverage-v4",
            "reason_code": "SOURCE_CHAIN_GLOBAL_CONTRADICTION",
            "rejection_count": 1,
            "max_rewrites": 1,
            "rewrite_available": True,
            "recovery_mode": "answer_rewrite",
            "coverage": {
                "required_endpoint_count": 2,
                "covered_endpoint_count": 2,
                "required_stage_count": 3,
                "covered_stage_count": 3,
                "required_evidence_slot_count": 4,
                "covered_evidence_slot_count": 4,
                "unique_source_paths": 4,
                "complete": True,
                "schema": "workspace-source-chain-coverage-v3",
            },
            "answer_denied_global_coverage": True,
            "uncertainty_clause_count": 1,
        }
        assert failures[0].internal["run_checkpoint"]["resume_node"] == "call_model"
        assert "agent.run.completed" in [event.event_type for event in envelopes]
        assert "agent.run.failed" not in [event.event_type for event in envelopes]
        completed = next(event for event in envelopes if event.event_type == "agent.run.completed")
        assert "这部分不知道" in completed.runtime_event["payload"]["output"]
        assert model._call_count == 2

    def test_final_answer_rewrite_fails_closed_after_one_retry_with_safe_details(self):
        observations = [
            _trusted_source_observation(
                1, "apps/web/src/api/tasks.ts", "return apiPost('/tasks', input)"
            ),
            _trusted_source_observation(
                2,
                "runtime/outbox/publisher.py",
                "EVENT_TO_STREAM = {'task.created': 'run-queue'}",
            ),
            _trusted_source_observation(
                3,
                "runtime/consumer.py",
                "client.xreadgroup(streams={RUN_QUEUE: '>'})",
            ),
            _trusted_source_observation(
                4,
                "runtime/worker.py",
                "self._process_job_with_cancel_check(job)",
            ),
        ]
        state = AgentState(
            task_id="task-1",
            run_id="run-1",
            user_goal="请阅读这个代码库，说明 Web 到 Worker 的真实调用链和文件依据。",
            observations=observations,
            iteration=1,
        )
        model = _FixedActionModelProvider(AgentAction.finish("证据不足，完整调用链整体无法确认。"))
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            final_answer_validators=(WorkspaceSourceChainCoverageValidator(),),
            worker_id="test",
            max_iterations=1,
        )

        envelopes = runner.run(
            _make_job(state.user_goal),
            _initial_state=state,
            _emit_run_started=False,
        )

        failures = [event for event in envelopes if event.event_type == "model.call.failed"]
        assert len(failures) == 2
        assert failures[0].runtime_event["payload"]["recoverable"] is True
        assert failures[1].runtime_event["payload"]["recoverable"] is False
        terminal = next(event for event in envelopes if event.event_type == "agent.run.failed")
        details = terminal.runtime_event["payload"]["error"]["details"]
        assert details["answer_validation"]["reason_code"] == ("SOURCE_CHAIN_GLOBAL_CONTRADICTION")
        assert details["answer_validation"]["rejection_count"] == 2
        assert details["answer_validation"]["rewrite_available"] is False
        serialized = json.dumps(details, ensure_ascii=False)
        assert "证据不足" not in serialized
        assert "runtime/worker.py" not in serialized
        assert model._call_count == 2

    def test_source_chain_action_guard_rejects_redundant_covered_component_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "worker.py").write_text(
                "def run_worker():\n"
                "    client.xreadgroup(streams={RUN_QUEUE: '>'})\n"
                "    self._process_job_with_cancel_check(job)\n",
                encoding="utf-8",
            )
            redundant = Path(tmpdir, "runtime", "runner.py")
            redundant.parent.mkdir(parents=True)
            redundant.write_text("def run_again():\n    self._runner.run(job)\n", encoding="utf-8")
            gateway_path = Path(tmpdir, "apps", "web", "gateway.py")
            gateway_path.parent.mkdir(parents=True)
            gateway_path.write_text(
                "def create_task():\n"
                "    return apiPost('/tasks', input)\n"
                "EVENT_TO_STREAM = {'task.created': 'run-queue'}\n",
                encoding="utf-8",
            )
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "apps/web/gateway.py"},
                        "读取入口和生产端",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "apps/web/gateway.py"},
                        "继续加深已覆盖入口",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "worker.py"},
                        "改为补齐 Worker 和消费端",
                    ),
                    AgentAction.finish("入口、传输和 Worker 终点均有源码依据。"),
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                final_answer_validators=(WorkspaceSourceChainCoverageValidator(),),
                worker_id="test",
                max_iterations=4,
            )

            envelopes = runner.run(
                _make_job(
                    "请阅读这个代码库，说明 Web 到 Worker 的真实调用链和文件依据。",
                    workspace_path=tmpdir,
                )
            )

        event_types = [event.event_type for event in envelopes]
        assert event_types.count("tool.call.finished") == 2
        assert event_types.count("model.call.failed") == 1
        planning_failure = next(
            event.runtime_event["payload"]
            for event in envelopes
            if event.event_type == "model.call.failed"
        )
        assert planning_failure["error_code"] == "SOURCE_CHAIN_PLANNING_STALLED"
        assert planning_failure["recoverable"] is True
        assert "agent.run.completed" in event_types
        assert "agent.run.failed" not in event_types
        assert model._call_count == 4

    def test_source_chain_rules_are_dormant_without_codex_extension_validator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "worker.py").write_text(
                "def run_worker():\n"
                "    client.xreadgroup(streams={RUN_QUEUE: '>'})\n"
                "    self._process_job_with_cancel_check(job)\n",
                encoding="utf-8",
            )
            gateway_path = Path(tmpdir, "apps", "web", "gateway.py")
            gateway_path.parent.mkdir(parents=True)
            gateway_path.write_text(
                "def create_task():\n"
                "    return apiPost('/tasks', input)\n"
                "EVENT_TO_STREAM = {'task.created': 'run-queue'}\n",
                encoding="utf-8",
            )
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "apps/web/gateway.py"},
                        "读取入口",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "apps/web/gateway.py"},
                        "再次核对入口",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "worker.py"},
                        "读取 Worker",
                    ),
                    AgentAction.finish("根据已读文件给出有限结论。"),
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
                max_iterations=4,
            )

            envelopes = runner.run(
                _make_job(
                    "请阅读这个代码库，说明 Web 到 Worker 的真实调用链和文件依据。",
                    workspace_path=tmpdir,
                )
            )

        event_types = [event.event_type for event in envelopes]
        assert event_types.count("tool.call.finished") == 2
        assert event_types.count("model.call.failed") == 1
        strategy_change = next(
            event.runtime_event["payload"]
            for event in envelopes
            if event.event_type == "model.call.failed"
        )
        assert strategy_change["error_code"] == "STRATEGY_CHANGE_REQUIRED"
        assert "agent.run.completed" in event_types
        assert "agent.run.failed" not in event_types

    def test_unsatisfied_source_read_rotates_to_less_attempted_evidence_slots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            weak_frontend = Path(tmpdir, "apps", "web", "src", "view.vue")
            weak_frontend.parent.mkdir(parents=True)
            weak_frontend.write_text("const title = 'Task'\n", encoding="utf-8")
            worker = Path(tmpdir, "runtime", "worker.py")
            worker.parent.mkdir(parents=True)
            worker.write_text(
                "def run_worker():\n    self._process_job_with_cancel_check(job)\n",
                encoding="utf-8",
            )
            publisher = Path(tmpdir, "runtime", "outbox", "publisher.py")
            publisher.parent.mkdir(parents=True)
            publisher.write_text(
                "EVENT_TO_STREAM = {'task.created': 'run-queue'}\n",
                encoding="utf-8",
            )
            consumer = Path(tmpdir, "runtime", "consumer.py")
            consumer.write_text(
                "def receive():\n    client.xreadgroup(streams={RUN_QUEUE: '>'})\n",
                encoding="utf-8",
            )
            strong_frontend = Path(tmpdir, "apps", "web", "src", "api.ts")
            strong_frontend.write_text(
                "export const create = (input) => apiPost('/tasks', input)\n",
                encoding="utf-8",
            )
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "apps/web/src/view.vue"},
                        "先读前端候选",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "runtime/worker.py"},
                        "轮转到 Worker",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "runtime/outbox/publisher.py"},
                        "轮转到生产端",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "runtime/consumer.py"},
                        "轮转到消费端",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "apps/web/src/api.ts"},
                        "回到仍缺失的前端调用边",
                    ),
                    AgentAction.finish("所有固定证据槽均有直接源码依据。"),
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                final_answer_validators=(WorkspaceSourceChainCoverageValidator(),),
                worker_id="test",
                max_iterations=5,
            )

            envelopes = runner.run(
                _make_job(
                    "请阅读这个代码库，说明 Web 到 Worker 的真实调用链和文件依据。",
                    workspace_path=tmpdir,
                )
            )

        event_types = [event.event_type for event in envelopes]
        assert event_types.count("tool.call.finished") == 5
        assert "model.call.failed" not in event_types
        assert "agent.run.completed" in event_types
        assert "agent.run.failed" not in event_types

    def test_source_chain_guard_retries_are_bounded_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            web_gateway = Path(tmpdir, "apps", "web", "gateway.py")
            web_gateway.parent.mkdir(parents=True)
            web_gateway.write_text(
                "def create_task():\n"
                "    return apiPost('/tasks', input)\n"
                "EVENT_TO_STREAM = {'task.created': 'run-queue'}\n",
                encoding="utf-8",
            )
            worker_path = Path(tmpdir, "runtime", "worker.py")
            worker_path.parent.mkdir(parents=True)
            worker_path.write_text(
                "def run_worker():\n"
                "    client.xreadgroup(streams={RUN_QUEUE: '>'})\n"
                "    self._process_job_with_cancel_check(job)\n",
                encoding="utf-8",
            )
            redundant_guarded = AgentAction.call_tool(
                "workspace.read_file",
                {"path": "apps/web/gateway.py"},
                "重复加深已覆盖入口",
            )
            broad_discovery = AgentAction.call_tool(
                "workspace.list_files",
                {"path": "."},
                "保守发现未知位置",
            )
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "workspace.read_file",
                        {"path": "apps/web/gateway.py"},
                        "读取入口和传输",
                    ),
                    redundant_guarded,
                    redundant_guarded,
                    redundant_guarded,
                    broad_discovery,
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                final_answer_validators=(WorkspaceSourceChainCoverageValidator(),),
                worker_id="test",
                max_iterations=14,
            )

            envelopes = runner.run(
                _make_job(
                    "请阅读这个代码库，说明 Web 到 Worker 的真实调用链和文件依据。",
                    workspace_path=tmpdir,
                )
            )

        event_types = [event.event_type for event in envelopes]
        planning_failures = [
            event
            for event in envelopes
            if event.event_type == "model.call.failed"
            and event.runtime_event["payload"].get("error_code") == "SOURCE_CHAIN_PLANNING_STALLED"
        ]
        assert event_types.count("tool.call.finished") == 1
        assert len(planning_failures) == 2
        navigation = planning_failures[0].runtime_event["payload"]["navigation_guard"]
        assert navigation["policy_version"] == "source-navigation-v5"
        assert navigation["reason_code"] == "REPEATED_SOURCE_ACTION"
        assert navigation["tool_class"] == "read"
        assert "path" not in navigation
        assert "query" not in navigation
        terminal = next(
            event.runtime_event["payload"]["error"]
            for event in envelopes
            if event.event_type == "agent.run.failed"
        )
        assert terminal["code"] == "SOURCE_CHAIN_NAVIGATION_STALLED"
        assert terminal["details"]["source_navigation"]["reason_code"] == ("REPEATED_SOURCE_ACTION")
        assert "apps/web/gateway.py" not in json.dumps(terminal["details"], ensure_ascii=False)
        assert "agent.run.completed" not in event_types
        assert model._call_count == 4

    def test_model_calling_tool_after_explicit_budget_exhaustion_fails_without_effect(self):
        action = AgentAction.call_tool(
            "workspace.list_files",
            {"path": "."},
            "继续调用工具",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_FixedActionModelProvider(action),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
                max_iterations=1,
            )
            envelopes = runner.run(_make_job("列出文件", workspace_path=tmpdir))

        event_types = [event.event_type for event in envelopes]
        assert event_types.count("tool.call.started") == 1
        failures = [
            event.runtime_event["payload"]["error"]
            for event in envelopes
            if event.event_type == "agent.run.failed"
        ]
        assert failures[0]["code"] == "MAX_ITERATIONS"

    def test_graph_recursion_limit_scales_with_runtime_tool_budget(self):
        model = _FixedActionModelProvider(
            [
                *[
                    AgentAction.call_tool(
                        "workspace.list_files",
                        {"path": f"part-{index}"},
                        "继续收集不同范围的证据",
                    )
                    for index in range(6)
                ],
                AgentAction.finish("证据收集完成"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            for index in range(6):
                Path(tmpdir, f"part-{index}").mkdir()
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
                max_iterations=10,
            )
            envelopes = runner.run(_make_job("多步骤只读任务", workspace_path=tmpdir))

        event_types = [event.event_type for event in envelopes]
        assert event_types.count("tool.call.started") == 6
        assert "agent.run.completed" in event_types
        assert "agent.run.failed" not in event_types

    @pytest.mark.parametrize("max_iterations", (0, -1, 21, True))
    def test_max_iterations_rejects_unbounded_or_invalid_values(self, max_iterations):
        with pytest.raises(ValueError, match="max_iterations"):
            AgentRunner(
                model_provider=MockModelProvider(),
                tool_gateway=_make_tool_gateway(),
                max_iterations=max_iterations,
            )

    def test_max_iterations_accepts_exact_upper_boundary(self):
        runner = AgentRunner(
            model_provider=MockModelProvider(),
            tool_gateway=_make_tool_gateway(),
            max_iterations=20,
        )

        assert isinstance(runner, AgentRunner)

    def test_envelope_validation(self):
        """所有事件通过 envelope 校验。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = _make_agent_runner()
            job = _make_job("列出文件", workspace_path=tmpdir)
            envelopes = runner.run(job)

            for env in envelopes:
                env.validate()
                assert env.event_type == env.runtime_event["type"]

    # -- Phase 6: action validation hardening --

    def test_unknown_action_type_failed(self):
        """未知 action_type → agent.run.failed（不再 fallback 为 completed）。"""
        bad_action = AgentAction(action_type="unknown_type")  # type: ignore[arg-type]
        model = _FixedActionModelProvider(bad_action)
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "agent.run.started" in event_types
        assert "agent.run.failed" in event_types
        assert "agent.run.completed" not in event_types  # 关键：不应 completed
        assert "tool.call.started" not in event_types  # 关键：不应发起工具

        failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        err = failed.runtime_event["payload"]["error"]
        assert err["code"] == "INVALID_AGENT_ACTION"
        assert err["category"] == "runtime"
        assert err["recoverable"] is False

    def test_call_tool_empty_tool_name_failed(self):
        """call_tool 但 tool_name 为空 → agent.run.failed，不出现 tool.call.started。"""
        bad_action = AgentAction(action_type="call_tool", tool_name="", arguments={})
        model = _FixedActionModelProvider(bad_action)
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "agent.run.started" in event_types
        assert "agent.run.failed" in event_types
        assert "tool.call.started" not in event_types  # 关键：不应发起工具
        assert "agent.run.completed" not in event_types

        failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        err = failed.runtime_event["payload"]["error"]
        assert err["code"] == "INVALID_AGENT_ACTION"

    def test_call_tool_arguments_not_dict_failed(self):
        """call_tool 但 arguments 不是 dict → agent.run.failed。"""
        bad_action = AgentAction(
            action_type="call_tool",
            tool_name="workspace.list_files",
            arguments="not_a_dict",  # type: ignore[arg-type]
        )
        model = _FixedActionModelProvider(bad_action)
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "agent.run.started" in event_types
        assert "agent.run.failed" in event_types
        assert "tool.call.started" not in event_types  # 关键：不应发起工具
        assert "agent.run.completed" not in event_types

        failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        err = failed.runtime_event["payload"]["error"]
        assert err["code"] == "INVALID_AGENT_ACTION"

    def test_finish_empty_final_message_failed(self):
        """finish 但 final_message 为空 → agent.run.failed。"""
        bad_action = AgentAction(action_type="finish", final_message="")
        model = _FixedActionModelProvider(bad_action)
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "agent.run.started" in event_types
        assert "agent.run.failed" in event_types
        assert "agent.run.completed" not in event_types  # 关键：不应 completed

        failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        err = failed.runtime_event["payload"]["error"]
        assert err["code"] == "INVALID_AGENT_ACTION"

    def test_finish_whitespace_only_final_message_failed(self):
        """finish 但 final_message 只有空白字符 → agent.run.failed。"""
        bad_action = AgentAction(action_type="finish", final_message="   ")
        model = _FixedActionModelProvider(bad_action)
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "agent.run.failed" in event_types
        assert "agent.run.completed" not in event_types

    def test_valid_finish_still_works(self):
        """合法的 finish action 仍然正常工作。"""
        valid_action = AgentAction(action_type="finish", final_message="任务完成")
        model = _FixedActionModelProvider(valid_action)
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "agent.run.completed" in event_types
        assert "agent.run.failed" not in event_types

    def test_valid_call_tool_still_works(self):
        """合法的 call_tool action 仍然正常工作（先 call_tool 再 finish）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "readme.md").write_text("hello")
            # 第一轮 call_tool，第二轮 finish（模拟真实 AgentRunner 流程）
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.list_files",
                    arguments={"workspace_root": tmpdir, "path": "."},
                ),
                AgentAction(action_type="finish", final_message="文件列表获取成功"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("irrelevant", workspace_path=tmpdir)
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types
            assert "agent.run.completed" in event_types

    # -- Phase 6 补充：ModelProvider 返回值类型防御 --

    def _assert_invalid_action_failed(self, envelopes, model_type: str = ""):
        """统一断言非法 action 正确 terminal 到 agent.run.failed。"""
        event_types = [e.event_type for e in envelopes]
        assert "agent.run.started" in event_types, f"缺少 agent.run.started ({model_type})"
        assert "agent.run.failed" in event_types, f"缺少 agent.run.failed ({model_type})"
        assert "agent.run.completed" not in event_types, (
            f"不应有 agent.run.completed ({model_type})"
        )
        assert "tool.call.started" not in event_types, f"不应有 tool.call.started ({model_type})"

        failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        err = failed.runtime_event["payload"]["error"]
        assert err["code"] == "INVALID_AGENT_ACTION", (
            f"error.code 不是 INVALID_AGENT_ACTION ({model_type})"
        )
        assert err["category"] == "runtime", f"error.category 不是 runtime ({model_type})"
        assert err["recoverable"] is False, f"error.recoverable 不是 false ({model_type})"

    def test_model_returns_none_failed(self):
        """decide_next_action 返回 None → agent.run.failed。"""
        model = _FixedActionModelProvider(None)
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)
        self._assert_invalid_action_failed(envelopes, "None")

    def test_model_returns_dict_failed(self):
        """decide_next_action 返回 dict → agent.run.failed。"""
        model = _FixedActionModelProvider({"action_type": "finish"})
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)
        self._assert_invalid_action_failed(envelopes, "dict")

    def test_model_returns_str_failed(self):
        """decide_next_action 返回 str → agent.run.failed。"""
        model = _FixedActionModelProvider("finish")
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)
        self._assert_invalid_action_failed(envelopes, "str")

    def test_call_tool_non_string_tool_name_failed(self):
        """call_tool 但 tool_name 不是字符串（int）→ agent.run.failed。"""
        bad_action = AgentAction(
            action_type="call_tool",
            tool_name=123,  # type: ignore[arg-type]
            arguments={},
        )
        model = _FixedActionModelProvider(bad_action)
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)
        self._assert_invalid_action_failed(envelopes, "tool_name=int")

    def test_finish_non_string_final_message_failed(self):
        """finish 但 final_message 不是字符串（int）→ agent.run.failed。"""
        bad_action = AgentAction(
            action_type="finish",
            final_message=42,  # type: ignore[arg-type]
        )
        model = _FixedActionModelProvider(bad_action)
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        job = _make_job("irrelevant")
        envelopes = runner.run(job)
        self._assert_invalid_action_failed(envelopes, "final_message=int")

    def test_default_workspace_root_fallback(self):
        """job.workspace_path 为空时使用 default_workspace_root。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.md").write_text("x")

            runner = _make_agent_runner()
            job = _make_job("list files", workspace_path="")
            envelopes = runner.run(job, default_workspace_root=tmpdir)

            # 工具应成功执行
            finished = next((e for e in envelopes if e.event_type == "tool.call.finished"), None)
            assert finished is not None
            tc = finished.runtime_event["payload"]["tool_call"]
            assert tc["status"] == "completed"

    # -- Phase 6A: AgentRunner + workspace.read_file 集成 --

    def test_read_file_success_flow(self):
        """AgentRunner 执行 read_file 成功：started → tool.started → tool.finished → run.completed。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "AGENTS.md").write_text("# AGENTS\n\nTest document.")

            # 第一轮 read_file，第二轮 finish
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.read_file",
                    arguments={"workspace_root": tmpdir, "path": "AGENTS.md"},
                ),
                AgentAction(action_type="finish", final_message="文件读取成功"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("读取 AGENTS.md", workspace_path=tmpdir)
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types
            assert "agent.run.completed" in event_types

            # 验证 tool.call.started 的 tool_name
            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            assert (
                started.runtime_event["payload"]["tool_call"]["tool_name"] == "workspace.read_file"
            )

            # 验证 tool.call.finished 包含 result.data 和 content_summary
            finished = next(e for e in envelopes if e.event_type == "tool.call.finished")
            tc = finished.runtime_event["payload"]["tool_call"]
            assert tc["status"] == "completed"
            assert tc["result"]["kind"] == "text"
            assert tc["result"]["data"]["path"] == "AGENTS.md"
            assert "# AGENTS" in tc["result"]["data"]["content"]
            # 验证 content_summary 存在（Phase 6A runner 适配）
            if "content_summary" in finished.runtime_event["payload"]:
                cs = finished.runtime_event["payload"]["content_summary"]
                assert cs["path"] == "AGENTS.md"
                assert cs["chars_read"] > 0

    def test_read_file_not_found_failed(self):
        """read_file 文件不存在 → tool.call.failed + agent.run.failed。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.read_file",
                    arguments={"workspace_root": tmpdir, "path": "missing.md"},
                ),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("读取 missing.md", workspace_path=tmpdir)
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "tool.call.started" in event_types
            assert "tool.call.failed" in event_types
            assert "agent.run.failed" in event_types
            assert "agent.run.completed" not in event_types

            # 验证错误码
            failed = next(e for e in envelopes if e.event_type == "tool.call.failed")
            assert failed.runtime_event["payload"]["tool_call"]["error"]["code"] == "FILE_NOT_FOUND"

    def test_read_file_not_affect_list_files(self):
        """原有 list_files 测试不受 read_file 影响。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "readme.md").write_text("hello")

            runner = _make_agent_runner()
            job = _make_job("列出文件", workspace_path=tmpdir)
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types
            assert "agent.run.completed" in event_types

            # list_files 工具应被调用
            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            assert (
                started.runtime_event["payload"]["tool_call"]["tool_name"] == "workspace.list_files"
            )


# ============================================================
# MockRunner + AgentRunner 集成
# ============================================================


class TestMockRunnerAgentRunnerIntegration:
    """MockRunner 委托 AgentRunner 的集成测试。"""

    def test_tool_scenario_delegates_to_agent_runner(self):
        """MockRunner 在 tool scenario 下委托 AgentRunner 执行。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "readme.md").write_text("hello")

            gateway = _make_tool_gateway()
            agent_runner = _make_agent_runner(gateway)
            runner = MockRunner(
                worker_id="w1",
                agent_runner=agent_runner,
                default_workspace_root=tmpdir,
            )

            job = _make_job("列出当前 workspace 文件")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types
            assert "agent.run.completed" in event_types

    def test_simple_success_not_affected_by_agent_runner(self):
        """普通任务仍走 simple_success（不走 AgentRunner）。"""
        gateway = _make_tool_gateway()
        agent_runner = _make_agent_runner(gateway)
        runner = MockRunner(worker_id="w1", agent_runner=agent_runner)

        job = _make_job("hello world")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "model.delta" in event_types
        assert "agent.run.completed" in event_types
        assert "tool.call.started" not in event_types

    def test_permission_scenario_not_affected(self):
        """permission scenario 不受 agent_runner 影响。"""
        gateway = _make_tool_gateway()
        agent_runner = _make_agent_runner(gateway)
        runner = MockRunner(worker_id="w1", agent_runner=agent_runner)

        job = _make_job("test permission scenario")
        envelopes = runner.run_with_cancel_check(
            job,
            wait_decision=lambda req_id: "deny",
            prepare_wait=lambda req_id: None,
        )

        event_types = [e.event_type for e in envelopes]
        assert "permission.required" in event_types
        assert "tool.call.started" in event_types  # shell tool.call.started

    def test_tool_scenario_cancel_via_agent_runner(self):
        """cancel 从 MockRunner 传给 AgentRunner。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            gateway = _make_tool_gateway()
            agent_runner = _make_agent_runner(gateway)
            runner = MockRunner(
                worker_id="w1",
                agent_runner=agent_runner,
                default_workspace_root=tmpdir,
            )

            job = _make_job("列出文件")
            # 第一次 cancel_check 就返回 True
            envelopes = runner.run_with_cancel_check(
                job,
                cancel_check=lambda: True,
            )

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.cancelled" in event_types
            assert "agent.run.completed" not in event_types

    def test_tool_scenario_fallback_to_legacy(self):
        """无 agent_runner 但有 tool_gateway 时回退到旧 _do_tool_run。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "x.txt").write_text("x")
            gateway = _make_tool_gateway()
            # 不传 agent_runner，只传 tool_gateway
            runner = MockRunner(
                worker_id="w1",
                tool_gateway=gateway,
                default_workspace_root=tmpdir,
            )

            job = _make_job("列出文件")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.finished" in event_types
            assert "agent.run.completed" in event_types

    def test_no_gateway_no_agent_runner_falls_back(self):
        """无 agent_runner 无 tool_gateway → simple_success。"""
        runner = MockRunner(worker_id="w1")
        job = _make_job("列出文件")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "model.delta" in event_types
        assert "agent.run.completed" in event_types

    # -- Phase 6A: read_file Web 入口路由 --

    def test_read_file_chinese_routes_to_agent_runner(self):
        """ "读取 AGENTS.md" → MockRunner 委托 AgentRunner → workspace.read_file。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "AGENTS.md").write_text("# AGENTS\n\nTest file.")

            gateway = _make_tool_gateway()
            agent_runner = _make_agent_runner(gateway)
            runner = MockRunner(
                worker_id="w1",
                agent_runner=agent_runner,
                default_workspace_root=tmpdir,
            )

            job = _make_job("读取 AGENTS.md")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types
            assert "agent.run.completed" in event_types

            # 验证 tool_name 是 workspace.read_file
            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            tc = started.runtime_event["payload"]["tool_call"]
            assert tc["tool_name"] == "workspace.read_file"

            # 验证 content_summary 存在
            finished = next(e for e in envelopes if e.event_type == "tool.call.finished")
            if "content_summary" in finished.runtime_event["payload"]:
                cs = finished.runtime_event["payload"]["content_summary"]
                assert cs["path"] == "AGENTS.md"

    def test_read_file_english_routes_to_agent_runner(self):
        """ "read CLAUDE.md" → MockRunner 委托 AgentRunner → workspace.read_file。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "CLAUDE.md").write_text("# CLAUDE\n\nTest.")

            gateway = _make_tool_gateway()
            agent_runner = _make_agent_runner(gateway)
            runner = MockRunner(
                worker_id="w1",
                agent_runner=agent_runner,
                default_workspace_root=tmpdir,
            )

            job = _make_job("read CLAUDE.md")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types

            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            assert (
                started.runtime_event["payload"]["tool_call"]["tool_name"] == "workspace.read_file"
            )

    def test_summarize_routes_to_agent_runner(self):
        """ "总结 AGENTS.md" → MockRunner 委托 AgentRunner → workspace.read_file。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "AGENTS.md").write_text("# Project\n\nSummary content.")

            gateway = _make_tool_gateway()
            agent_runner = _make_agent_runner(gateway)
            runner = MockRunner(
                worker_id="w1",
                agent_runner=agent_runner,
                default_workspace_root=tmpdir,
            )

            job = _make_job("总结 AGENTS.md")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            assert "tool.call.finished" in event_types

            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            assert (
                started.runtime_event["payload"]["tool_call"]["tool_name"] == "workspace.read_file"
            )

    def test_read_file_without_filename_not_routed(self):
        """ "总结文档"（无文件名）→ 不走 tool scenario，走 simple_success。"""
        gateway = _make_tool_gateway()
        agent_runner = _make_agent_runner(gateway)
        runner = MockRunner(worker_id="w1", agent_runner=agent_runner)

        job = _make_job("总结文档内容")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        # 应走 simple_success 而非 tool scenario
        assert "model.delta" in event_types
        assert "tool.call.started" not in event_types

    def test_list_files_still_routes_correctly(self):
        """原有 list_files 路由不受 read_file 影响。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "readme.md").write_text("hello")

            gateway = _make_tool_gateway()
            agent_runner = _make_agent_runner(gateway)
            runner = MockRunner(
                worker_id="w1",
                agent_runner=agent_runner,
                default_workspace_root=tmpdir,
            )

            job = _make_job("列出文件")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            # list_files 触发 → tool_name 应为 workspace.list_files
            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            assert (
                started.runtime_event["payload"]["tool_call"]["tool_name"] == "workspace.list_files"
            )

    def test_permission_scenario_still_priority_over_read_file(self):
        """permission scenario 优先级仍高于 read_file tool scenario。"""
        gateway = _make_tool_gateway()
        agent_runner = _make_agent_runner(gateway)
        runner = MockRunner(worker_id="w1", agent_runner=agent_runner)

        job = _make_job("test permission scenario for read AGENTS.md")
        envelopes = runner.run_with_cancel_check(
            job,
            wait_decision=lambda req_id: "deny",
            prepare_wait=lambda req_id: None,
        )

        event_types = [e.event_type for e in envelopes]
        assert "permission.required" in event_types
        # permission scenario 的 tool.call.started 是 shell（不是 read_file）
        started = next(e for e in envelopes if e.event_type == "tool.call.started")
        assert started.runtime_event["payload"]["tool_call"]["tool_name"] == "shell"

    # -- Phase 6A 补测：查看 / view / 打开 入口路由 + 一致性 --

    def test_view_chinese_content_routes_to_read_file(self):
        """ "查看 AGENTS.md 内容" → MockRunner 委托 AgentRunner → workspace.read_file。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "AGENTS.md").write_text("# AGENTS\n\nTest.")

            gateway = _make_tool_gateway()
            agent_runner = _make_agent_runner(gateway)
            runner = MockRunner(
                worker_id="w1",
                agent_runner=agent_runner,
                default_workspace_root=tmpdir,
            )

            job = _make_job("查看 AGENTS.md 内容")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            assert (
                started.runtime_event["payload"]["tool_call"]["tool_name"] == "workspace.read_file"
            )
            assert "agent.run.completed" in event_types

    def test_view_english_routes_to_read_file(self):
        """ "view AGENTS.md" → MockRunner 委托 AgentRunner → workspace.read_file。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "AGENTS.md").write_text("# AGENTS")

            gateway = _make_tool_gateway()
            agent_runner = _make_agent_runner(gateway)
            runner = MockRunner(
                worker_id="w1",
                agent_runner=agent_runner,
                default_workspace_root=tmpdir,
            )

            job = _make_job("view AGENTS.md")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            assert (
                started.runtime_event["payload"]["tool_call"]["tool_name"] == "workspace.read_file"
            )

    def test_open_routes_to_read_file(self):
        """ "打开 AGENTS.md" → MockRunner 委托 AgentRunner → workspace.read_file。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "AGENTS.md").write_text("# AGENTS")

            gateway = _make_tool_gateway()
            agent_runner = _make_agent_runner(gateway)
            runner = MockRunner(
                worker_id="w1",
                agent_runner=agent_runner,
                default_workspace_root=tmpdir,
            )

            job = _make_job("打开 AGENTS.md")
            envelopes = runner.run(job)

            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types
            started = next(e for e in envelopes if e.event_type == "tool.call.started")
            assert (
                started.runtime_event["payload"]["tool_call"]["tool_name"] == "workspace.read_file"
            )

    def test_mock_runner_and_model_provider_read_file_consistency(self):
        """验证 MockRunner 和 MockModelProvider 对 read_file 意图判断一致。

        对同一输入，MockRunner._is_read_file_intent 返回 True 的 case，
        MockModelProvider.decide_next_action 也必须返回 workspace.read_file（而不是 finish）。
        """
        from tests.testing_doubles import READ_FILE_KEYWORDS as SHARED_KW

        # 所有共享关键词 + 文件名都应被两者识别
        test_files = ["AGENTS.md", "CLAUDE.md", "docs/README.md"]
        for kw in SHARED_KW:
            for fname in test_files:
                goal = f"{kw} {fname}"
                # MockRunner side
                job = _make_job(goal)
                assert MockRunner._is_read_file_intent(job), (
                    f"MockRunner 未识别 read_file 意图: goal={goal!r}"
                )
                # MockModelProvider side
                model = MockModelProvider()
                state = AgentState(
                    task_id="t1",
                    run_id="r1",
                    user_goal=goal,
                    workspace_root="/tmp",
                )
                action = model.decide_next_action(state)
                assert action.action_type == "call_tool", (
                    f"MockModelProvider 未生成 call_tool: goal={goal!r}"
                )
                assert action.tool_name == "workspace.read_file", (
                    f"MockModelProvider tool_name 不是 read_file: goal={goal!r}"
                )


# ============================================================
# Phase 6B-0 审查修复：workspace_root 信任边界安全测试
# ============================================================


class TestWorkspaceRootTrustBoundary:
    """AgentRunner 必须从 AgentState 注入可信 workspace_root，
    覆盖模型可能通过 AgentAction.arguments 传入的任意值。"""

    def test_agent_runner_injects_workspace_root_from_job(self):
        """AgentRunner 从 job.workspace_path 注入 workspace_root。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.list_files",
                    arguments={"path": "."},
                ),
                AgentAction(action_type="finish", final_message="done"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("list", workspace_path=tmpdir)
            envelopes = runner.run(job)

            finished = next((e for e in envelopes if e.event_type == "tool.call.finished"), None)
            assert finished is not None

            # tool.call.started 的 arguments_summary 应显示可信 workspace_root
            started = next((e for e in envelopes if e.event_type == "tool.call.started"), None)
            assert started is not None
            summary = started.runtime_event["payload"]["tool_call"]["arguments_summary"]
            assert summary["workspace_root"] == tmpdir

    def test_agent_runner_uses_default_workspace_root(self):
        """job.workspace_path 为空时使用 default_workspace_root。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.md").write_text("x")
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.list_files",
                    arguments={"path": "."},
                ),
                AgentAction(action_type="finish", final_message="done"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("list", workspace_path="")
            envelopes = runner.run(job, default_workspace_root=tmpdir)

            finished = next((e for e in envelopes if e.event_type == "tool.call.finished"), None)
            assert finished is not None

    def test_malicious_model_workspace_root_overridden(self):
        """绕过 Parser 直接返回 malicious workspace_root="/" 的 AgentAction 时，
        AgentRunner 必须使用可信 state.workspace_root 覆盖。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "safe.txt").write_text("safe content")
            # 恶意模型尝试访问根目录
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.read_file",
                    arguments={"workspace_root": "/", "path": "safe.txt"},
                ),
                AgentAction(action_type="finish", final_message="done"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("read", workspace_path=tmpdir)
            envelopes = runner.run(job)

            # AgentRunner 应覆盖 workspace_root 为 tmpdir（可信值）
            # 因此工具应成功执行（使用 tmpdir 而非 /）
            finished = next((e for e in envelopes if e.event_type == "tool.call.finished"), None)
            assert finished is not None, (
                f"预期 tool.call.finished（AgentRunner 覆盖 workspace_root 为 {tmpdir}），"
                f"但未找到，事件类型: {[e.event_type for e in envelopes]}"
            )

            # 验证 tool.call.started 的 arguments_summary 显示的是可信 workspace_root 而非 "/"
            started = next((e for e in envelopes if e.event_type == "tool.call.started"), None)
            assert started is not None
            summary = started.runtime_event["payload"]["tool_call"]["arguments_summary"]
            assert summary["workspace_root"] == tmpdir, (
                f"arguments_summary.workspace_root 应为可信值 {tmpdir}，"
                f"实际: {summary['workspace_root']}"
            )

    def test_malicious_model_list_files_root_overridden(self):
        """恶意模型 list_files 的 workspace_root="/" 也被覆盖。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.list_files",
                    arguments={"workspace_root": "/", "path": "."},
                ),
                AgentAction(action_type="finish", final_message="done"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("list", workspace_path=tmpdir)
            envelopes = runner.run(job)

            finished = next((e for e in envelopes if e.event_type == "tool.call.finished"), None)
            assert finished is not None, f"AgentRunner 应覆盖 workspace_root 为可信值 {tmpdir}"

    def test_empty_workspace_root_still_fail_closed(self):
        """workspace_root 为空时仍然 fail closed。"""
        runner = _make_agent_runner()
        job = _make_job("列出文件", workspace_path="")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "tool.call.failed" in event_types
        assert "agent.run.failed" in event_types

        failed = next(e for e in envelopes if e.event_type == "tool.call.failed")
        tc = failed.runtime_event["payload"]["tool_call"]
        assert tc["error"]["code"] == "WORKSPACE_ROOT_REQUIRED"

    def test_observation_includes_tool_name(self):
        """AgentRunner 写入的 observation 包含 tool_name 字段。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.list_files",
                    arguments={"path": "."},
                ),
                AgentAction(action_type="finish", final_message="done"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("list", workspace_path=tmpdir)
            envelopes = runner.run(job)

            # 验证 observation 中有 tool_name
            finished = next((e for e in envelopes if e.event_type == "tool.call.finished"), None)
            assert finished is not None
            tc = finished.runtime_event["payload"]["tool_call"]
            assert tc["tool_name"] == "workspace.list_files"


class TestMockModelProviderDoesNotLeakWorkspaceRoot:
    """MockModelProvider 不再自行把 state.workspace_root 放进 AgentAction.arguments。"""

    def test_list_files_without_workspace_root(self):
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="列出当前 workspace 文件",
            workspace_root="/secure/path",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert "workspace_root" not in action.arguments, (
            "MockModelProvider 不应在 arguments 中包含 workspace_root，该字段由 AgentRunner 注入"
        )
        assert "path" in action.arguments

    def test_read_file_without_workspace_root(self):
        model = MockModelProvider()
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="读取 AGENTS.md",
            workspace_root="/secure/path",
        )
        action = model.decide_next_action(state)
        assert action.action_type == "call_tool"
        assert "workspace_root" not in action.arguments, (
            "MockModelProvider 不应在 arguments 中包含 workspace_root"
        )
        assert action.arguments["path"] == "AGENTS.md"


# ============================================================
# Phase 6B-0 审查修复 v2：observation 历史信息测试
# ============================================================


class TestObservationHistory:
    """AgentRunner observation 包含 tool_call_id 和 model_action。"""

    def test_tool_call_ids_consistent(self):
        """tool.call.started、tool.call.finished 使用一致的 tool_call_id。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.md").write_text("x")
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.read_file",
                    arguments={"path": "test.md"},
                ),
                AgentAction(action_type="finish", final_message="done"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("read", workspace_path=tmpdir)
            envelopes = runner.run(job)

            started = next((e for e in envelopes if e.event_type == "tool.call.started"), None)
            finished = next((e for e in envelopes if e.event_type == "tool.call.finished"), None)
            assert started is not None
            assert finished is not None
            assert (
                started.runtime_event["payload"]["tool_call"]["id"]
                == finished.runtime_event["payload"]["tool_call"]["id"]
            )

    def test_malicious_workspace_root_still_overridden(self):
        """恶意 ModelProvider workspace_root="/" 仍被可信值覆盖。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "safe.txt").write_text("safe")
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.read_file",
                    arguments={"workspace_root": "/", "path": "safe.txt"},
                ),
                AgentAction(action_type="finish", final_message="done"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("read", workspace_path=tmpdir)
            envelopes = runner.run(job)

            finished = next((e for e in envelopes if e.event_type == "tool.call.finished"), None)
            assert finished is not None, f"AgentRunner 应覆盖 workspace_root 为可信值 {tmpdir}"
            started = next((e for e in envelopes if e.event_type == "tool.call.started"), None)
            assert started is not None
            summary = started.runtime_event["payload"]["tool_call"]["arguments_summary"]
            assert summary["workspace_root"] == tmpdir

    def test_empty_workspace_root_fail_closed(self):
        """workspace_root 为空时仍然 fail closed。"""
        runner = _make_agent_runner()
        job = _make_job("列出文件", workspace_path="")
        envelopes = runner.run(job)

        event_types = [e.event_type for e in envelopes]
        assert "tool.call.failed" in event_types
        assert "agent.run.failed" in event_types

        failed = next(e for e in envelopes if e.event_type == "tool.call.failed")
        tc = failed.runtime_event["payload"]["tool_call"]
        assert tc["error"]["code"] == "WORKSPACE_ROOT_REQUIRED"


class TestObservationModelAction:
    """observation 中的 model_action 字段验证。"""

    def test_model_action_via_direct_runner_call(self):
        """通过直接构造的 AgentRunner + call_tool action 验证 model_action。

        使用 _FixedActionModelProvider 产生 call_tool，
        然后检查 AgentRunner 在 observation 中写入的 model_action 不含 workspace_root。
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "safe.txt").write_text("safe")
            # 使用一个检查点：model 返回带 workspace_root="/" 的 AgentAction
            actions = [
                AgentAction(
                    action_type="call_tool",
                    tool_name="workspace.read_file",
                    arguments={"workspace_root": "/", "path": "safe.txt"},
                ),
                AgentAction(action_type="finish", final_message="done"),
            ]
            model = _FixedActionModelProvider(actions)
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            job = _make_job("read", workspace_path=tmpdir)
            envelopes = runner.run(job)

            # 工具应成功（AgentRunner 覆盖 workspace_root 为 tmpdir）
            finished = next((e for e in envelopes if e.event_type == "tool.call.finished"), None)
            assert finished is not None

            # 验证 arguments_summary 使用的是可信 workspace_root
            started = next((e for e in envelopes if e.event_type == "tool.call.started"), None)
            assert started is not None
            summary = started.runtime_event["payload"]["tool_call"]["arguments_summary"]
            assert summary["workspace_root"] == tmpdir, (
                f"arguments_summary 应为可信 workspace_root={tmpdir}，"
                f"实际={summary['workspace_root']}"
            )


# ============================================================
# Phase 6B-1 审查修复：ModelProviderError → agent.run.failed
# ============================================================


class TestProviderErrorToRunFailed:
    """AgentRunner 捕获 ModelProviderError 并转换为 agent.run.failed。"""

    def test_model_timeout_produces_run_failed(self):
        from jarvis_worker.agent.models.errors import model_timeout_error

        class _FailingProvider(ModelProvider):
            def decide_next_action(self, state):
                raise model_timeout_error("timeout")

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_FailingProvider(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))
            event_types = [e.event_type for e in envelopes]
            assert "agent.run.started" in event_types
            assert "agent.run.failed" in event_types
            assert "agent.run.completed" not in event_types
            failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
            err = failed.runtime_event["payload"]["error"]
            assert err["code"] == "MODEL_TIMEOUT"

    def test_model_http_error_produces_run_failed(self):
        from jarvis_worker.agent.models.errors import model_http_error

        class _FailingProvider(ModelProvider):
            def decide_next_action(self, state):
                raise model_http_error(500, "server error")

        runner = AgentRunner(
            model_provider=_FailingProvider(),
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        envelopes = runner.run(_make_job("test"))
        failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        assert failed.runtime_event["payload"]["error"]["code"] == "MODEL_HTTP_ERROR"

    def test_model_output_invalid_produces_run_failed(self):
        from jarvis_worker.agent.models.errors import model_output_invalid

        class _FailingProvider(ModelProvider):
            def decide_next_action(self, state):
                raise model_output_invalid("bad output")

        runner = AgentRunner(
            model_provider=_FailingProvider(),
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        envelopes = runner.run(_make_job("test"))
        failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        assert failed.runtime_event["payload"]["error"]["code"] == "MODEL_OUTPUT_INVALID"
        model_failures = [e for e in envelopes if e.event_type == "model.call.failed"]
        assert len(model_failures) == 2
        assert model_failures[0].runtime_event["payload"]["recoverable"] is True
        assert model_failures[1].runtime_event["payload"]["recoverable"] is False

    def test_model_output_invalid_gets_one_runtime_self_correction(self):
        from jarvis_worker.agent.models.errors import model_output_invalid

        class _RecoversProvider(ModelProvider):
            def __init__(self):
                self.calls = 0

            def decide_next_action(self, state):
                self.calls += 1
                if self.calls == 1:
                    raise model_output_invalid("bad output", failure_kind="invalid_json")
                assert "AgentAction" in state.effect_guard_feedback
                return AgentAction(action_type="finish", final_message="已完成")

        provider = _RecoversProvider()
        runner = AgentRunner(
            model_provider=provider,
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        envelopes = runner.run(_make_job("test"))

        assert provider.calls == 2
        assert [e.event_type for e in envelopes].count("model.call.failed") == 1
        assert (
            next(e for e in envelopes if e.event_type == "model.call.failed").runtime_event[
                "payload"
            ]["recoverable"]
            is True
        )
        assert any(e.event_type == "agent.run.completed" for e in envelopes)
        assert not any(e.event_type == "agent.run.failed" for e in envelopes)

    def test_unexpected_exception_produces_internal_error(self):
        class _FailingProvider(ModelProvider):
            def decide_next_action(self, state):
                raise RuntimeError("unexpected boom")

        runner = AgentRunner(
            model_provider=_FailingProvider(),
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        envelopes = runner.run(_make_job("test"))
        failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        err = failed.runtime_event["payload"]["error"]
        assert err["code"] == "MODEL_PROVIDER_INTERNAL_ERROR"
        # 原始异常不进入 RuntimeEvent
        assert "RuntimeError" not in err["message"]
        assert "unexpected boom" not in err["message"]

    def test_provider_error_uses_apperror_shape(self):
        from jarvis_worker.agent.models.errors import model_config_error

        class _FailingProvider(ModelProvider):
            def decide_next_action(self, state):
                raise model_config_error("bad config")

        runner = AgentRunner(
            model_provider=_FailingProvider(),
            tool_gateway=_make_tool_gateway(),
            worker_id="test",
        )
        envelopes = runner.run(_make_job("test"))
        failed = next(e for e in envelopes if e.event_type == "agent.run.failed")
        err = failed.runtime_event["payload"]["error"]
        assert "code" in err
        assert "message" in err
        assert "category" in err
        assert "recoverable" in err

    def test_mock_provider_not_broken(self):
        """MockModelProvider 仍正常工作。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = _make_agent_runner()
            envelopes = runner.run(_make_job("hello world", workspace_path=tmpdir))
            event_types = [e.event_type for e in envelopes]
            assert "agent.run.completed" in event_types


# ============================================================
# MockRunner 真实模式路由
# ============================================================


class TestMockRunnerRealMode:
    """openai_compatible 模式下所有任务进入 AgentRunner。"""

    def test_normal_goal_uses_agent_runner(self):
        """真实模式下普通目标仍调用 AgentRunner。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = MockRunner(
                worker_id="test",
                tool_gateway=_make_tool_gateway(),
                agent_runner=_make_agent_runner(),
                dev_mock_scenarios_enabled=False,
            )
            envelopes = runner.run(_make_job("随便说点什么", workspace_path=tmpdir))
            # 应走 AgentRunner → MockModelProvider → finish
            event_types = [e.event_type for e in envelopes]
            assert "agent.run.completed" in event_types
            assert "agent.run.failed" not in event_types

    def test_tool_goal_uses_agent_runner(self):
        """真实模式下工具目标仍调用 AgentRunner。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = MockRunner(
                worker_id="test",
                tool_gateway=_make_tool_gateway(),
                agent_runner=_make_agent_runner(),
                dev_mock_scenarios_enabled=False,
            )
            envelopes = runner.run(_make_job("列出文件", workspace_path=tmpdir))
            # 应走 AgentRunner → MockModelProvider → call_tool → finish
            event_types = [e.event_type for e in envelopes]
            assert "tool.call.started" in event_types

    def test_permission_keyword_still_uses_agent_runner(self):
        """真实模式下含"权限"关键词的普通目标仍调用 AgentRunner（不触发 DEV mock）。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = MockRunner(
                worker_id="test",
                tool_gateway=_make_tool_gateway(),
                agent_runner=_make_agent_runner(),
                dev_mock_scenarios_enabled=False,
            )
            envelopes = runner.run(_make_job("帮我看看权限设置", workspace_path=tmpdir))
            # 不应走 DEV permission scenario（simple_success → completed）
            event_types = [e.event_type for e in envelopes]
            assert "agent.run.completed" in event_types
            assert "permission.required" not in event_types

    def test_mock_mode_permission_still_works(self):
        """Mock 模式下 DEV permission scenario 仍正常工作。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = MockRunner(
                worker_id="test",
                tool_gateway=_make_tool_gateway(),
                agent_runner=_make_agent_runner(),
                dev_mock_scenarios_enabled=True,
            )
            envelopes = runner.run(_make_job("permission test", workspace_path=tmpdir))
            event_types = [e.event_type for e in envelopes]
            # DEV mock 模式：permission → simple_success（因为 wait_decision=None）
            assert "agent.run.completed" in event_types

    def test_provider_error_returns_run_failed(self):
        """Provider 异常返回 agent.run.failed。"""
        from jarvis_worker.agent.models.errors import model_timeout_error

        class _FailingProvider(ModelProvider):
            def decide_next_action(self, state):
                raise model_timeout_error("timeout")

        with tempfile.TemporaryDirectory() as tmpdir:
            ar = AgentRunner(
                model_provider=_FailingProvider(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            runner = MockRunner(
                worker_id="test",
                tool_gateway=_make_tool_gateway(),
                agent_runner=ar,
                dev_mock_scenarios_enabled=False,
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))
            event_types = [e.event_type for e in envelopes]
            assert "agent.run.failed" in event_types
            assert "agent.run.completed" not in event_types


class SpyAgentRunner:
    """记录 run() 调用信息的 AgentRunner 包装。"""

    def __init__(self, inner: AgentRunner):
        self._inner = inner
        self.run_count = 0
        self.goals: list[str] = []

    def run(self, job, default_workspace_root="", cancel_check=None):
        self.run_count += 1
        self.goals.append(job.user_goal)
        return self._inner.run(
            job, default_workspace_root=default_workspace_root, cancel_check=cancel_check
        )


class TestSpyRouting:
    """使用 SpyAgentRunner 精确验证路由。"""

    def test_normal_goal_calls_agent_runner_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ar = _make_agent_runner()
            spy = SpyAgentRunner(ar)
            runner = MockRunner(
                worker_id="test",
                tool_gateway=_make_tool_gateway(),
                agent_runner=spy,
                dev_mock_scenarios_enabled=False,
            )
            runner.run(_make_job("随便聊聊", workspace_path=tmpdir))
            assert spy.run_count == 1
            assert "随便聊聊" in spy.goals[0]

    def test_tool_goal_calls_agent_runner_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ar = _make_agent_runner()
            spy = SpyAgentRunner(ar)
            runner = MockRunner(
                worker_id="test",
                tool_gateway=_make_tool_gateway(),
                agent_runner=spy,
                dev_mock_scenarios_enabled=False,
            )
            runner.run(_make_job("列出文件", workspace_path=tmpdir))
            assert spy.run_count == 1

    def test_permission_keyword_calls_agent_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ar = _make_agent_runner()
            spy = SpyAgentRunner(ar)
            runner = MockRunner(
                worker_id="test",
                tool_gateway=_make_tool_gateway(),
                agent_runner=spy,
                dev_mock_scenarios_enabled=False,
            )
            envelopes = runner.run(_make_job("帮我看看权限设置", workspace_path=tmpdir))
            assert spy.run_count == 1
            # 不触发 DEV permission scenario
            event_types = [e.event_type for e in envelopes]
            assert "permission.required" not in event_types

    def test_mock_mode_still_works(self):
        """Mock 模式下 Spy 也可能被调用（tool keyword），但不影响 DEV scenario。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ar = _make_agent_runner()
            spy = SpyAgentRunner(ar)
            runner = MockRunner(
                worker_id="test",
                tool_gateway=_make_tool_gateway(),
                agent_runner=spy,
                dev_mock_scenarios_enabled=True,
            )
            envelopes = runner.run(_make_job("permission test", workspace_path=tmpdir))
            # DEV mock mode: falls through to simple_success (wait_decision=None)
            event_types = [e.event_type for e in envelopes]
            assert "agent.run.completed" in event_types


# ============================================================
# Phase 6B-2: model.call.* observability tests
# ============================================================


def _event_types(envs):
    return [e.event_type for e in envs]


def _find_event(envs, event_type):
    return next((e for e in envs if e.event_type == event_type), None)


def _assert_model_payload_safe(payload):
    """model.call.* payload 不得包含敏感信息。"""
    payload_str = str(payload).lower()
    forbidden = [
        "sk-",
        "authorization",
        "bearer",
        "prompt",
        "raw_response",
        "raw response",
        "headers",
        "api_key",
        "apikey",
        "secret",
        "token",
    ]
    for kw in forbidden:
        assert kw not in payload_str, f"payload 包含禁止关键词: {kw!r}"


class TestModelCallObservability:
    """model.call.started / completed / failed 事件覆盖。"""

    def test_success_path_emits_started_and_completed(self):
        """成功调用 → model.call.started + model.call.completed。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = _make_agent_runner()
            job = _make_job("hello world", workspace_path=tmpdir)
            envelopes = runner.run(job)
            types = _event_types(envelopes)
            assert "model.call.started" in types
            assert "model.call.completed" in types
            assert "agent.run.completed" in types

            started = _find_event(envelopes, "model.call.started")
            assert started is not None
            sp = started.runtime_event["payload"]
            assert sp["provider"] == "mock"
            assert sp["model_name"] == "mock"
            assert isinstance(sp["call_id"], str) and sp["call_id"]
            _assert_model_payload_safe(sp)

            completed = _find_event(envelopes, "model.call.completed")
            assert completed is not None
            cp = completed.runtime_event["payload"]
            assert cp["provider"] == "mock"
            assert isinstance(cp["duration_ms"], int) and cp["duration_ms"] >= 0
            assert cp["action_type"] == "finish"
            _assert_model_payload_safe(cp)

    def test_model_timeout_emits_failed(self):
        """ProviderError → model.call.started + model.call.failed + agent.run.failed。"""
        from jarvis_worker.agent.models.errors import model_timeout_error
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _Failing(MP):
            provider_name = "test-provider"
            model_name = "test-model"

            def decide_next_action(self, state):
                raise model_timeout_error("timeout")

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_Failing(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))
            types = _event_types(envelopes)
            assert "model.call.started" in types
            assert "model.call.failed" in types
            assert "agent.run.failed" in types
            assert "agent.run.completed" not in types
            assert types.count("model.call.failed") == 1
            assert types.count("agent.run.failed") == 1

            failed = _find_event(envelopes, "model.call.failed")
            fp = failed.runtime_event["payload"]
            assert fp["error_code"] == "MODEL_TIMEOUT"
            assert fp["recoverable"] is True
            assert isinstance(fp["duration_ms"], int) and fp["duration_ms"] >= 0
            assert fp["provider"] == "test-provider"
            _assert_model_payload_safe(fp)
            terminal = _find_event(envelopes, "agent.run.failed")
            assert terminal.runtime_event["payload"]["error"]["recoverable"] is True

    def test_structured_output_failure_metadata_is_observable_and_safe(self):
        """结构化失败只暴露分类和尝试次数，不暴露原始模型输出。"""
        from jarvis_worker.agent.models.errors import model_output_invalid
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _Failing(MP):
            provider_name = "test-provider"
            model_name = "test-model"

            def decide_next_action(self, state):
                error = model_output_invalid(
                    "模型输出解析失败",
                    failure_kind="missing_field",
                )
                error.attempt_count = 2
                raise error

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_Failing(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))

        failed = _find_event(envelopes, "model.call.failed")
        payload = failed.runtime_event["payload"]
        assert payload["error_code"] == "MODEL_OUTPUT_INVALID"
        assert payload["output_failure_kind"] == "missing_field"
        assert payload["attempt_count"] == 2
        assert "retry_instruction" not in payload

    def test_non_agent_action_emits_failed(self):
        """返回 dict → model.call.failed + agent.run.failed（含 model.call.started）。"""
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _ReturnsDict(MP):
            provider_name = "bad"
            model_name = "bad"

            def decide_next_action(self, state):
                return {"action_type": "sing"}

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_ReturnsDict(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))
            types = _event_types(envelopes)
            assert "model.call.started" in types
            assert "model.call.failed" in types
            assert "agent.run.failed" in types

            failed = _find_event(envelopes, "model.call.failed")
            fp = failed.runtime_event["payload"]
            assert fp["error_code"] == "INVALID_AGENT_ACTION"
            assert fp["recoverable"] is False
            assert isinstance(fp["duration_ms"], int) and fp["duration_ms"] >= 0
            _assert_model_payload_safe(fp)

    def test_unexpected_exception_emits_failed_safe(self):
        """RuntimeError → model.call.failed 不含异常原文。"""
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _Boom(MP):
            provider_name = "boom"
            model_name = "boom"

            def decide_next_action(self, state):
                raise RuntimeError("unexpected boom")

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_Boom(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))
            types = _event_types(envelopes)
            assert "model.call.failed" in types
            assert "agent.run.failed" in types

            failed = _find_event(envelopes, "model.call.failed")
            fp = failed.runtime_event["payload"]
            assert fp["error_code"] == "MODEL_PROVIDER_INTERNAL_ERROR"
            assert fp["recoverable"] is False
            # 不能出现 "unexpected boom" 或 "RuntimeError"
            payload_str = str(fp)
            assert "unexpected boom" not in payload_str
            assert "RuntimeError" not in payload_str
            _assert_model_payload_safe(fp)

    def test_call_tool_produces_completed_with_tool_call_action(self):
        """call_tool action → completed.action_type == "tool_call"。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            runner = _make_agent_runner()
            job = _make_job("列出文件", workspace_path=tmpdir)
            envelopes = runner.run(job)
            completed = _find_event(envelopes, "model.call.completed")
            assert completed is not None
            # 第一轮返回 call_tool
            cp = completed.runtime_event["payload"]
            assert cp["action_type"] == "tool_call"


class TestModelCallFieldValidation:
    """字段级非法 action → model.call.failed（不发布 model.call.completed）。"""

    def test_finish_empty_final_message_emits_failed(self):
        """finish.final_message 为空 → model.call.failed + agent.run.failed。"""
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _BadFinish(MP):
            provider_name = "test"
            model_name = "test"

            def decide_next_action(self, state):
                return AgentAction(action_type="finish", final_message="")

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_BadFinish(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))
            types = _event_types(envelopes)
            assert "model.call.started" in types
            assert "model.call.failed" in types
            assert "agent.run.failed" in types
            assert "model.call.completed" not in types

            failed = _find_event(envelopes, "model.call.failed")
            fp = failed.runtime_event["payload"]
            assert fp["error_code"] == "INVALID_AGENT_ACTION"
            assert fp["recoverable"] is False
            _assert_model_payload_safe(fp)

    def test_call_tool_empty_tool_name_emits_failed(self):
        """call_tool.tool_name 为空 → model.call.failed，不发布 tool.call.started。"""
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _BadTool(MP):
            provider_name = "test"
            model_name = "test"

            def decide_next_action(self, state):
                return AgentAction(action_type="call_tool", tool_name="", arguments={"p": "."})

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_BadTool(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))
            types = _event_types(envelopes)
            assert "model.call.started" in types
            assert "model.call.failed" in types
            assert "agent.run.failed" in types
            assert "model.call.completed" not in types
            assert "tool.call.started" not in types

    def test_call_tool_arguments_not_dict_emits_failed(self):
        """call_tool.arguments 不是 dict → model.call.failed。"""
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _BadArgs(MP):
            provider_name = "test"
            model_name = "test"

            def decide_next_action(self, state):
                return AgentAction(
                    action_type="call_tool", tool_name="workspace.list_files", arguments="bad"
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_BadArgs(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))
            types = _event_types(envelopes)
            assert "model.call.failed" in types
            assert "model.call.completed" not in types
            assert "tool.call.started" not in types

    def test_unknown_action_type_emits_failed(self):
        """未知 action_type → model.call.failed，不发布 completed。"""
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _UnknownAct(MP):
            provider_name = "test"
            model_name = "test"

            def decide_next_action(self, state):
                return AgentAction(action_type="fly_to_moon", final_message="??")

        with tempfile.TemporaryDirectory() as tmpdir:
            runner = AgentRunner(
                model_provider=_UnknownAct(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("test", workspace_path=tmpdir))
            types = _event_types(envelopes)
            assert "model.call.failed" in types
            assert "model.call.completed" not in types
            assert "agent.run.failed" in types


# ============================================================
# Phase 6C: LLM 真实工具调用闭环
# ============================================================


class TestLLMToolCallLoop:
    """完整闭环：LLM call_tool → ToolGateway → observation → LLM finish。"""

    def test_two_step_fake_provider_call_tool_then_finish(self):
        """第一轮 call_tool → 工具执行 → 第二轮 finish（含工具结果总结）。"""
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _TwoStep(MP):
            provider_name = "test-loop"
            model_name = "test-loop"

            def __init__(self):
                self._step = 0

            def decide_next_action(self, state):
                if self._step == 0:
                    self._step = 1
                    return AgentAction(
                        action_type="call_tool",
                        tool_name="workspace.list_files",
                        arguments={},
                        reason="用户要求列出文件",
                    )
                return AgentAction(
                    action_type="finish",
                    final_message=f"workspace 包含 {len(state.observations[0].get('data', {}).get('entries', []))} 个条目",
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "README.md").write_text("readme")
            (Path(tmpdir) / "docs").mkdir()
            runner = AgentRunner(
                model_provider=_TwoStep(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("列出文件", workspace_path=tmpdir))
            types = _event_types(envelopes)
            # 第一轮
            assert "model.call.started" in types
            assert "model.call.completed" in types
            assert "tool.call.started" in types
            assert "tool.call.finished" in types
            assert "agent.run.completed" in types
            assert "agent.run.failed" not in types

            # 验证 model.call.completed 的 action_type
            completed_events = [e for e in envelopes if e.event_type == "model.call.completed"]
            assert len(completed_events) >= 2  # 至少两轮 model call
            assert completed_events[0].runtime_event["payload"]["action_type"] == "tool_call"
            assert completed_events[1].runtime_event["payload"]["action_type"] == "finish"

            # 验证 workspace_root 由 AgentRunner 注入（不在 call_tool arguments 中）
            started = _find_event(envelopes, "tool.call.started")
            tc = started.runtime_event["payload"]["tool_call"]
            assert tc["arguments_summary"]["workspace_root"] == tmpdir

            # 最终 output 包含工具结果总结
            run_completed = _find_event(envelopes, "agent.run.completed")
            assert run_completed is not None
            output = run_completed.runtime_event["payload"]["output"]
            assert "2" in output or "条目" in output

    def test_two_step_with_read_file(self):
        """read_file → tool → finish 闭环。"""
        from jarvis_worker.agent.models.provider import ModelProvider as MP

        class _ReadThenFinish(MP):
            provider_name = "test-loop"
            model_name = "test-loop"

            def __init__(self):
                self._step = 0

            def decide_next_action(self, state):
                if self._step == 0:
                    self._step = 1
                    return AgentAction(
                        action_type="call_tool",
                        tool_name="workspace.read_file",
                        arguments={"path": "README.md"},
                        reason="用户要求读文件",
                    )
                return AgentAction(action_type="finish", final_message="文件内容已读取")

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "README.md").write_text("# Hello World\n\nTest document.")
            runner = AgentRunner(
                model_provider=_ReadThenFinish(),
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("读取 README.md", workspace_path=tmpdir))
            types = _event_types(envelopes)
            assert "tool.call.started" in types
            assert "tool.call.finished" in types
            assert "agent.run.completed" in types
            assert "agent.run.failed" not in types

            # 工具执行经过 ToolGateway（通过 tool.call.finished 验证）
            finished = _find_event(envelopes, "tool.call.finished")
            assert finished is not None
            tc = finished.runtime_event["payload"]["tool_call"]
            assert tc["status"] == "completed"
            assert tc["tool_name"] == "workspace.read_file"
            assert tc["run_id"] == finished.run_id
            assert tc["step_id"] == finished.runtime_event["step_id"]
            assert tc["provider"] == "native"
            assert tc["risk_level"] == "L0"
            assert tc["permission_status"] == "not_required"
            assert tc["arguments"]["workspace_root"] == tmpdir
            assert tc["arguments"]["path"] == "README.md"

    def test_two_step_with_search_files(self):
        """search_files → L0 tool → observation → finish 闭环。"""

        class _SearchThenFinish(ModelProvider):
            provider_name = "test-loop"
            model_name = "test-loop"

            def __init__(self):
                self.call_count = 0
                self.second_call_observation = None

            def decide_next_action(self, state):
                self.call_count += 1
                if self.call_count == 1:
                    return AgentAction(
                        action_type="call_tool",
                        tool_name="workspace.search_files",
                        arguments={"query": ".md", "path": ".", "max_results": 10},
                        reason="查找 Markdown 文件",
                    )
                self.second_call_observation = state.observations[-1]
                return AgentAction(
                    action_type="finish",
                    final_message="已找到 Markdown 文件",
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "README.md").write_text("readme")
            model = _SearchThenFinish()
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=ToolGateway(create_tool_registry(), PermissionManager()),
                worker_id="test",
            )

            envelopes = runner.run(_make_job("查找 Markdown 文件", workspace_path=tmpdir))

        types = _event_types(envelopes)
        assert model.call_count == 2
        assert model.second_call_observation is not None
        assert model.second_call_observation["data"]["matches"][0]["path"] == "README.md"
        assert "permission.required" not in types
        assert "tool.call.started" in types
        assert "tool.call.finished" in types
        assert "agent.run.completed" in types
        assert "agent.run.failed" not in types

        started = _find_event(envelopes, "tool.call.started")
        tool_call = started.runtime_event["payload"]["tool_call"]
        assert tool_call["tool_name"] == "workspace.search_files"
        assert tool_call["risk_level"] == "L0"
        assert tool_call["permission_status"] == "not_required"
        assert tool_call["arguments"]["workspace_root"] == tmpdir

    def test_two_step_with_get_file_info(self):
        """get_file_info → L0 tool → bounded observation → finish 闭环。"""

        class _InfoThenFinish(ModelProvider):
            provider_name = "test-loop"
            model_name = "test-loop"

            def __init__(self):
                self.call_count = 0
                self.second_call_observation = None

            def decide_next_action(self, state):
                self.call_count += 1
                if self.call_count == 1:
                    return AgentAction(
                        action_type="call_tool",
                        tool_name="workspace.get_file_info",
                        arguments={"path": "README.md"},
                        reason="查看 README.md 元信息",
                    )
                self.second_call_observation = state.observations[-1]
                return AgentAction(
                    action_type="finish",
                    final_message="已获取 README.md 元信息",
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "README.md").write_text("hello")
            model = _InfoThenFinish()
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=ToolGateway(create_tool_registry(), PermissionManager()),
                worker_id="test",
            )

            envelopes = runner.run(
                _make_job("查看 README.md 的类型、大小和修改时间", workspace_path=tmpdir)
            )

        types = _event_types(envelopes)
        assert model.call_count == 2
        assert model.second_call_observation is not None
        assert model.second_call_observation["data"]["path"] == "README.md"
        assert model.second_call_observation["data"]["type"] == "file"
        assert model.second_call_observation["data"]["size_bytes"] == 5
        assert "permission.required" not in types
        assert "tool.call.started" in types
        assert "tool.call.finished" in types
        assert "agent.run.completed" in types
        assert "agent.run.failed" not in types

        started = _find_event(envelopes, "tool.call.started")
        tool_call = started.runtime_event["payload"]["tool_call"]
        assert tool_call["tool_name"] == "workspace.get_file_info"
        assert tool_call["risk_level"] == "L0"
        assert tool_call["permission_status"] == "not_required"
        assert tool_call["arguments"]["workspace_root"] == tmpdir


# ============================================================
# Phase 6C 审查修复：Provider 组合回归 + PromptBuilder 边界
# ============================================================


class TestProviderComboWithToolCallLoop:
    """真实 Provider（MockTransport）完整工具调用闭环。"""

    def test_two_round_httpx_mock_transport(self, monkeypatch):
        """两轮 MockTransport：call_tool → ToolGateway → observation → finish。"""
        monkeypatch.setenv("TEST_KEY", "sk-test")
        import httpx

        call_count = [0]

        def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            body = json.loads(request.content)
            messages = body.get("messages", [])
            if call_count[0] == 1:
                # 第一轮：验证请求不含 API key，验证消息结构
                for m in messages:
                    assert "sk-" not in json.dumps(m)
                raw = json.dumps(
                    {
                        "action_type": "call_tool",
                        "tool_name": "workspace.list_files",
                        "arguments": {},
                        "reason": "用户要求列出文件",
                    }
                )
                return httpx.Response(
                    200,
                    content=(
                        "data: "
                        + json.dumps(
                            {"choices": [{"delta": {"content": raw}, "finish_reason": None}]}
                        )
                        + "\n\n"
                        + "data: "
                        + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                        + "\n\n"
                        + "data: [DONE]\n\n"
                    ).encode(),
                )
            else:
                # 第二轮：验证自定义 AgentAction JSON 历史和受控 Runtime observation
                roles = [m.get("role") for m in messages]
                assert "tool" not in roles, f"自定义 JSON 协议不应伪装原生 tool role: {roles}"
                assert not any(
                    "tool_calls" in m for m in messages if m.get("role") == "assistant"
                ), "自定义 JSON 协议不应发送供应商原生 tool_calls"
                runtime_messages = [
                    m
                    for m in messages
                    if m.get("role") == "user"
                    and str(m.get("content", "")).startswith("[Runtime ToolResult")
                ]
                assert len(runtime_messages) == 1
                runtime_payload = json.loads(runtime_messages[0]["content"].split("\n", 1)[1])
                assert runtime_payload["runtime_message_type"] == "tool_result"
                assert runtime_payload["tool_name"] == "workspace.list_files"
                assert runtime_payload["tool_call_id"]
                tool_content = runtime_payload["result"]
                assert tool_content["tool_name"] == "workspace.list_files"
                assert tool_content["ok"] is True
                assert isinstance(tool_content.get("data"), dict), "observation data 应为 dict"
                entries = tool_content["data"].get("entries", [])
                assert isinstance(entries, list), "data.entries 应为 list"
                entry_names = [e.get("name") for e in entries if isinstance(e, dict)]
                assert "README.md" in entry_names, f"entries 应包含 README.md, got {entry_names}"
                # observation 不含 workspace_root
                assert "workspace_root" not in json.dumps(tool_content)
                raw = json.dumps(
                    {
                        "action_type": "finish",
                        "final_message": "workspace 列出 1 个文件：README.md",
                    }
                )
                return httpx.Response(
                    200,
                    content=(
                        "data: "
                        + json.dumps(
                            {"choices": [{"delta": {"content": raw}, "finish_reason": None}]}
                        )
                        + "\n\n"
                        + "data: "
                        + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]})
                        + "\n\n"
                        + "data: [DONE]\n\n"
                    ).encode(),
                )

        provider = OpenAiCompatibleModelProvider(
            base_url="https://api.test.example/v1",
            model="test-model",
            api_key_env="TEST_KEY",
            prompt_builder=PromptBuilder(),
            timeout=5.0,
            max_retries=0,
            max_tokens=100,
            _client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
            _sleeper=lambda s: None,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "README.md").write_text("test")
            runner = AgentRunner(
                model_provider=provider,
                tool_gateway=_make_tool_gateway(),
                worker_id="test",
            )
            envelopes = runner.run(_make_job("列出文件", workspace_path=tmpdir))
            types = _event_types(envelopes)

            assert call_count[0] == 2, f"应为 2 次 HTTP 请求，实际: {call_count[0]}"

            # 严格事件顺序
            expected = [
                "agent.run.started",
                "model.call.started",
                "model.context.prepared",
                "model.call.completed",  # action_type=tool_call
                "tool.call.started",
                "tool.call.finished",
                "model.call.started",
                "model.context.prepared",
                "model.delta",
                "model.call.completed",  # action_type=finish
                "artifact.created",
                "agent.run.completed",
            ]
            assert types == expected, f"事件顺序不符:\n期望: {expected}\n实际: {types}"

            # 验证 action_type
            completed = [e for e in envelopes if e.event_type == "model.call.completed"]
            assert completed[0].runtime_event["payload"]["action_type"] == "tool_call"
            assert completed[1].runtime_event["payload"]["action_type"] == "finish"
            artifact = _find_event(envelopes, "artifact.created").runtime_event["payload"][
                "artifact"
            ]
            assert artifact["purpose"] == "final_response"
            assert artifact["producer"] == {"type": "runtime"}
            assert "is_final_output" not in artifact["metadata"]

            # workspace_root 来自 AgentRunner 注入
            started = _find_event(envelopes, "tool.call.started")
            assert (
                started.runtime_event["payload"]["tool_call"]["arguments_summary"]["workspace_root"]
                == tmpdir
            )

            # 最终 output
            run_comp = _find_event(envelopes, "agent.run.completed")
            assert "README.md" in run_comp.runtime_event["payload"]["output"]

            # 安全：RuntimeEvent 不含任何敏感内容
            for e in envelopes:
                ps = json.dumps(e.runtime_event["payload"]).lower()
                for kw in (
                    "sk-test",
                    "authorization",
                    "bearer",
                    "api_key",
                    "apikey",
                    "headers",
                    "secret",
                    "prompt",
                    "raw_response",
                    "raw response",
                ):
                    assert kw not in ps, f"{e.event_type} payload 含敏感词: {kw}"


class TestPromptBuilderBoundary:
    """PromptBuilder allowed_tools 边界 + Provider 联动。"""

    def test_empty_allowed_tools_no_tool_names_in_prompt(self):
        b = PromptBuilder(allowed_tools=[])
        msgs = b.build_messages("列出文件")
        c = msgs[0].content
        assert "workspace.list_files" not in c
        assert "workspace.read_file" not in c
        assert "不要尝试调用任何工具" in c

    def test_empty_allowed_tools_provider_rejects_call_tool(self, monkeypatch):
        """allowed_tools=[] 时 Provider 拒绝 call_tool。"""
        monkeypatch.setenv("TEST_KEY", "sk-test")
        import httpx

        p = OpenAiCompatibleModelProvider(
            base_url="https://api.test.example/v1",
            model="t",
            api_key_env="TEST_KEY",
            prompt_builder=PromptBuilder(allowed_tools=[]),
            timeout=5.0,
            max_retries=0,
            max_tokens=100,
            _client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {
                                        "content": json.dumps(
                                            {
                                                "action_type": "call_tool",
                                                "tool_name": "workspace.list_files",
                                                "arguments": {},
                                            }
                                        )
                                    },
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                    )
                )
            ),
            _sleeper=lambda s: None,
        )
        with pytest.raises(ModelProviderError, match="解析失败|不在允许列表"):
            p.decide_next_action(AgentState(task_id="t", run_id="r", user_goal="hi"))

    def test_single_tool_list_files_only(self):
        b = PromptBuilder(
            allowed_tools=[
                {
                    "name": "workspace.list_files",
                    "description": "list files",
                    "parameters": {},
                }
            ]
        )
        msgs = b.build_messages("列出文件")
        c = msgs[0].content
        assert "workspace.list_files" in c
        assert "workspace.read_file" not in c

    def test_single_tool_provider_read_file_rejected(self, monkeypatch):
        """只有 list_files 时 read_file 被 parser 拒绝。"""
        monkeypatch.setenv("TEST_KEY", "sk-test")
        import httpx

        p = OpenAiCompatibleModelProvider(
            base_url="https://api.test.example/v1",
            model="t",
            api_key_env="TEST_KEY",
            prompt_builder=PromptBuilder(
                allowed_tools=[
                    {
                        "name": "workspace.list_files",
                        "description": "lf",
                        "parameters": {},
                    }
                ]
            ),
            timeout=5.0,
            max_retries=0,
            max_tokens=100,
            _client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {
                                        "content": json.dumps(
                                            {
                                                "action_type": "call_tool",
                                                "tool_name": "workspace.read_file",
                                                "arguments": {"path": "x"},
                                            }
                                        )
                                    },
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                    )
                )
            ),
            _sleeper=lambda s: None,
        )
        with pytest.raises(ModelProviderError, match="不在允许列表"):
            p.decide_next_action(AgentState(task_id="t", run_id="r", user_goal="hi"))

    def test_default_allowed_tools_unchanged(self):
        b = PromptBuilder()
        assert "workspace.list_files" in b.allowed_tool_names
        assert "workspace.read_file" in b.allowed_tool_names


class TestRequiredToolEvidenceGuard:
    """模型不得在明确工具任务中无成功 ToolResult 就直接 finish。"""

    def test_existing_target_short_circuits_conditional_create_without_permission(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            notes = Path(tmpdir, "notes")
            notes.mkdir()
            target = notes / "existing.md"
            target.write_text("原版本", encoding="utf-8")
            original = target.read_bytes()
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "workspace.search_files",
                        {"path": "notes", "query": "existing.md"},
                        "先检查条件式创建目标是否存在",
                    ),
                    AgentAction.finish("文件已创建并写入新版本。"),
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=ToolGateway(create_tool_registry(), PermissionManager()),
                worker_id="test",
                max_iterations=3,
            )
            goal = "创建 notes/existing.md，写入‘新版本’，如果已存在就先告诉我，不要覆盖。"

            events = runner.run(_make_job(goal, workspace_path=tmpdir))
            current = target.read_bytes()

        assert current == original
        assert "permission.required" not in [event.event_type for event in events]
        tool_names = [
            event.runtime_event["payload"]["tool_call"]["tool_name"]
            for event in events
            if event.event_type == "tool.call.finished"
        ]
        assert tool_names == ["workspace.search_files"]
        completed = next(event for event in events if event.event_type == "agent.run.completed")
        assert completed.runtime_event["payload"]["output"] == (
            "目标已存在，未执行创建或覆盖：`notes/existing.md`。原路径内容保持不变。"
        )

    def test_collection_evidence_same_query_in_narrower_path_cannot_close_gap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            procurement = Path(tmpdir, "procurement")
            requests = procurement / "requests"
            requests.mkdir(parents=True)
            (procurement / "policy.md").write_text(
                "金额超过 10000 元还必须由财务审批。\n",
                encoding="utf-8",
            )
            (procurement / "procedure.md").write_text(
                "提交后由部门负责人审批，再按政策判断财务审批，最后执行。\n",
                encoding="utf-8",
            )
            (requests / "PR-2026-017.md").write_text(
                "金额：12800 元\n当前状态：等待财务审批\n",
                encoding="utf-8",
            )
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "workspace.search_text",
                        {"path": ".", "query": "PR-2026-017"},
                        "定位申请锚点",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_files",
                        {"files": ["procurement/requests/PR-2026-017.md"]},
                        "读取申请记录",
                    ),
                    AgentAction.finish("只根据申请记录给出完整流程。"),
                    AgentAction.call_tool(
                        "workspace.search_files",
                        {"path": "procurement", "query": "PR-2026-017"},
                        "在更窄目录重复精确搜索",
                    ),
                    AgentAction.finish("仍只根据申请记录给出完整流程。"),
                    AgentAction.call_tool(
                        "workspace.list_files",
                        {"path": "procurement"},
                        "枚举相关父目录以扩展候选",
                    ),
                    AgentAction.call_tool(
                        "workspace.read_files",
                        {"files": ["procurement/policy.md", "procurement/procedure.md"]},
                        "读取互补政策与流程正文",
                    ),
                    AgentAction.finish("已根据申请、政策和流程三份正文完成核对。"),
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=ToolGateway(create_tool_registry(), PermissionManager()),
                worker_id="test",
                max_iterations=8,
            )
            goal = (
                "请阅读工作区中与采购申请 PR-2026-017 相关的材料，说明它从提交到执行的"
                "流程、目前停在哪一步。每一步都给出文件依据；证据不足就明确说不知道。"
            )
            intent = IntentExtraction(
                primary_intent="task",
                retrieval=RetrievalIntent(
                    mode="skip",
                    query="",
                    confidence=1.0,
                    reason="任务只需要工作区证据",
                ),
                workspace=IntentWorkspace(
                    evidence="required",
                    action="read",
                    ambiguity="clear",
                    reason="用户要求读取多份相关材料",
                ),
            )
            state = AgentState(
                task_id="task-1",
                run_id="run-1",
                user_goal=goal,
                intent=intent.to_state_dict(),
                intent_context=IntentRuntimeContext().to_state_dict(),
            )
            envelopes = runner.run(
                _make_job(goal, workspace_path=tmpdir),
                _initial_state=state,
            )

        failures = [
            event.runtime_event["payload"]
            for event in envelopes
            if event.event_type == "model.call.failed"
            and event.runtime_event["payload"].get("error_code") == "REQUIRED_TOOL_EVIDENCE_MISSING"
        ]
        assert len(failures) == 2
        tool_names = [
            event.runtime_event["payload"]["tool_call"]["tool_name"]
            for event in envelopes
            if event.event_type == "tool.call.finished"
        ]
        assert tool_names == [
            "workspace.search_text",
            "workspace.read_files",
            "workspace.search_files",
            "workspace.list_files",
            "workspace.read_files",
        ]
        completed = next(event for event in envelopes if event.event_type == "agent.run.completed")
        assert completed.runtime_event["payload"]["output"] == (
            "已根据申请、政策和流程三份正文完成核对。"
        )

    def test_conflicting_workspace_effect_is_corrected_before_permission(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "workspace.create_directory",
                        {"path": "notes"},
                        "先创建目标目录",
                    ),
                    AgentAction.call_tool(
                        "workspace.create_file",
                        {"path": "notes/private.md", "content": "不要保存"},
                        "创建用户明确要求的文件",
                    ),
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=ToolGateway(create_tool_registry(), PermissionManager()),
                worker_id="test",
                max_iterations=3,
            )
            job = _make_job(
                "创建 notes/private.md，写入‘不要保存’。",
                workspace_path=tmpdir,
            )

            events = runner.run(job, defer_permission=True)

            assert not (workspace / "notes").exists()
            failed = next(event for event in events if event.event_type == "model.call.failed")
            assert failed.runtime_event["payload"]["error_code"] == (
                "REQUIRED_TOOL_ACTION_MISMATCH"
            )
            assert failed.runtime_event["payload"]["recoverable"] is True
            permission = next(
                event for event in events if event.event_type == "permission.required"
            )
            request = permission.runtime_event["payload"]["request"]
            assert request["tool_name"] == "workspace.create_file"
            checkpoint = request["_internal_checkpoint"]
            assert checkpoint["state"]["effect_guard_rejections"] == 1
            assert "workspace.create_file" in checkpoint["state"]["effect_guard_feedback"]
            assert "workspace.create_directory" in checkpoint["state"]["effect_guard_feedback"]

    def test_premature_finish_retries_and_survives_permission_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "guarded.txt"
            model = _FixedActionModelProvider(
                [
                    AgentAction.finish("文件已成功创建"),
                    AgentAction.call_tool(
                        "workspace.create_file",
                        {"path": "guarded.txt", "content": "EVIDENCE_OK"},
                        "创建用户要求的文件",
                    ),
                    AgentAction.finish("已根据真实工具结果创建文件"),
                ]
            )
            runner = AgentRunner(
                model_provider=model,
                tool_gateway=ToolGateway(create_tool_registry(), PermissionManager()),
                worker_id="test",
                max_iterations=3,
            )
            job = _make_job(
                "请在当前工作区创建 guarded.txt，内容必须且只能是 EVIDENCE_OK，不要调用其他工具。",
                workspace_path=tmpdir,
            )

            initial = runner.run(job, defer_permission=True)

            assert not target.exists()
            assert "agent.run.completed" not in [event.event_type for event in initial]
            model_failed = next(
                event for event in initial if event.event_type == "model.call.failed"
            )
            assert model_failed.runtime_event["payload"] == {
                "provider": "unknown",
                "model_name": "unknown",
                "call_id": model_failed.runtime_event["payload"]["call_id"],
                "duration_ms": model_failed.runtime_event["payload"]["duration_ms"],
                "error_code": "REQUIRED_TOOL_EVIDENCE_MISSING",
                "recoverable": True,
            }
            permission = next(
                event for event in initial if event.event_type == "permission.required"
            )
            checkpoint = permission.runtime_event["payload"]["request"]["_internal_checkpoint"]
            assert checkpoint["state"]["effect_guard_rejections"] == 1
            assert "workspace.create_file" in checkpoint["state"]["effect_guard_feedback"]
            # 旧 checkpoint 可能携带此前源码补证计数；真实 ToolResult 必须统一消费它，
            # 否则权限恢复后会错误地再次进入 tool-required 模式并拒绝 finish。
            checkpoint["state"]["source_chain_guard_rejections"] = 1
            checkpoint["state"]["source_chain_evidence_rejections"] = 1
            # 真实 REC-06 还出现过 schema 合法但语义冲突的 Intent：明确创建
            # 文件却被冻结为 read+metadata。成功 create ToolResult 自带可信
            # path/size/hash，恢复后不得再要求额外 list/get_info 才能 finish。
            checkpoint["state"]["intent"]["workspace"].update(
                {"action": "read", "evidence": "metadata"}
            )
            checkpoint["state"]["completion_contract"].update(
                {"workspace_action": "read", "workspace_evidence": "metadata"}
            )

            resumed = runner.resume_permission(checkpoint, "allow_once")

            assert target.read_text() == "EVIDENCE_OK"
            resumed_types = [event.event_type for event in resumed]
            assert "tool.call.finished" in resumed_types
            assert "agent.run.completed" in resumed_types
            finished = next(event for event in resumed if event.event_type == "tool.call.finished")
            result = finished.runtime_event["payload"]["tool_call"]["result"]
            assert result["kind"] == "file"
            assert result["deliverables"][0]["path"] == "guarded.txt"
            assert len(result["deliverables"][0]["content_hash"]) == 64
            completed = next(
                event for event in resumed if event.event_type == "agent.run.completed"
            )
            assert completed.runtime_event["payload"]["output"] == "已根据真实工具结果创建文件"

    def test_repeated_premature_finish_fails_instead_of_claiming_success(self):
        model = _FixedActionModelProvider(AgentAction.finish("文件已成功创建"))
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=ToolGateway(create_tool_registry(), PermissionManager()),
            worker_id="test",
            max_iterations=3,
        )
        job = _make_job(
            "G2 d6053a5 Redis 状态丢失恢复：请创建 "
            "tmp/rc1-g2/redis-d6053a5.txt，内容必须且只能是 rc1-redis-d6053a5，"
            "不要调用其他工具。"
        )

        envelopes = runner.run(job)

        event_types = [event.event_type for event in envelopes]
        assert event_types.count("model.call.failed") == 3
        assert "tool.call.started" not in event_types
        assert "agent.run.completed" not in event_types
        failed = next(event for event in envelopes if event.event_type == "agent.run.failed")
        assert failed.runtime_event["payload"]["error"] == {
            "code": "MODEL_OUTPUT_INVALID",
            "message": "模型调用失败: 工具补证模式只允许 call_tool",
            "category": "runtime",
            "recoverable": False,
        }

    def test_failed_required_tool_keeps_real_tool_error_as_terminal_truth(self):
        registry = ToolRegistry()
        registry.register(
            ToolManifest(name="sample.write", risk_level_default="L1"),
            lambda _request: ToolResult(
                ok=False,
                summary="目标父目录不存在",
                error={
                    "code": "PARENT_DIRECTORY_NOT_FOUND",
                    "message": "目标父目录不存在",
                    "category": "tool",
                    "recoverable": True,
                },
            ),
        )
        runner = AgentRunner(
            model_provider=_FixedActionModelProvider(
                [
                    AgentAction.call_tool("sample.write", {}, "执行写入"),
                    AgentAction.finish("没有完成写入"),
                ]
            ),
            tool_gateway=ToolGateway(registry, PermissionManager()),
            worker_id="test",
            max_iterations=2,
        )

        events = runner.run(_make_job("请调用 sample.write 完成写入"))

        failed = next(event for event in events if event.event_type == "agent.run.failed")
        assert failed.runtime_event["payload"]["error"] == {
            "code": "PARENT_DIRECTORY_NOT_FOUND",
            "message": "目标父目录不存在",
            "category": "tool",
            "recoverable": False,
        }
        assert all(
            event.runtime_event["payload"].get("error", {}).get("code")
            != "REQUIRED_TOOL_NOT_EXECUTED"
            for event in events
        )

    def test_sensitive_persistence_intent_is_deterministically_refused_without_effect(self):
        class SensitiveKnowledgeIntent:
            uses_model = False

            def extract(self, *_args, **_kwargs):
                return IntentExtraction(
                    primary_intent="knowledge_write",
                    retrieval=RetrievalIntent(
                        mode="skip",
                        query="",
                        confidence=1.0,
                        reason="用户要求持久保存凭据",
                        document_scope="none",
                    ),
                    effects=IntentEffects(knowledge_write="required"),
                    source="rule",
                )

        executed = []
        registry = ToolRegistry()
        registry.register(
            ToolManifest(name="knowledge.create_document", risk_level_default="L1"),
            lambda _request: executed.append(True) or ToolResult(ok=True),
        )
        secret = "sk-proj-abcdefghijklmnopqrstuv"
        runner = AgentRunner(
            model_provider=_FixedActionModelProvider(
                AgentAction.call_tool(
                    "knowledge.create_document",
                    {"title": "凭据", "content": secret},
                    "保存以便以后使用",
                )
            ),
            tool_gateway=ToolGateway(registry, PermissionManager()),
            intent_extractor=SensitiveKnowledgeIntent(),
        )

        events = runner.run(_make_job(f"请记住 api_key={secret}，以后自动使用"))

        assert executed == []
        assert events[-1].event_type == "agent.run.completed"
        output = events[-1].runtime_event["payload"]["output"]
        assert "不能" in output and "环境变量" in output
        assert secret not in output
        assert not any(event.event_type.startswith("tool.call.") for event in events)

        events_en = runner.run(
            _make_job(f"Please answer in English. Remember api_key={secret} for later use."),
            memory_items=[
                {
                    "scope_type": "global",
                    "category": "preference",
                    "key": "response.language",
                    "content": "Use English responses by default.",
                }
            ],
        )

        assert executed == []
        assert events_en[-1].event_type == "agent.run.completed"
        output_en = events_en[-1].runtime_event["payload"]["output"]
        assert "can't save passwords" in output_en
        assert secret not in output_en
        assert "model.call.failed" not in [event.event_type for event in events_en]
        assert not any(event.event_type.startswith("tool.call.") for event in events_en)

    def test_sensitive_memory_request_overrides_unknown_intent_clarification(self):
        class UnknownIntent:
            uses_model = False

            def extract(self, *_args, **_kwargs):
                return IntentExtraction(
                    primary_intent="unknown",
                    retrieval=RetrievalIntent(
                        mode="skip",
                        query="",
                        confidence=0.0,
                        reason="无法确定意图",
                        document_scope="none",
                    ),
                    effects=IntentEffects(),
                    source="rule",
                )

        secret = "sk-proj-abcdefghijklmnopqrstuv"
        runner = AgentRunner(
            model_provider=_FixedActionModelProvider(AgentAction.finish("已经记住")),
            tool_gateway=ToolGateway(ToolRegistry(), PermissionManager()),
            intent_extractor=UnknownIntent(),
        )

        events = runner.run(_make_job(f"记住我的 API key 是 {secret}，以后自动使用"))

        assert events[-1].event_type == "agent.run.completed"
        output = events[-1].runtime_event["payload"]["output"]
        assert "不能" in output and "环境变量" in output
        assert "请补充具体目标" not in output
        assert secret not in output
        assert not any(event.event_type.startswith("tool.call.") for event in events)

    def test_identical_failed_effect_is_not_reauthorized_without_state_change(self):
        calls = []
        registry = ToolRegistry()
        registry.register(
            ToolManifest(
                name="sample.write",
                risk_level_default="L2",
                allowed_decisions=["allow_once", "deny"],
            ),
            lambda _request: (
                calls.append(True)
                or ToolResult(
                    ok=False,
                    summary="缺少目标容器",
                    error={
                        "code": "TARGET_CONTAINER_REQUIRED",
                        "message": "缺少目标容器",
                        "category": "tool",
                        "recoverable": True,
                    },
                )
            ),
        )
        action = AgentAction.call_tool("sample.write", {"name": "report"}, "写入")
        runner = AgentRunner(
            model_provider=_FixedActionModelProvider([action, action]),
            tool_gateway=ToolGateway(registry, PermissionManager()),
            max_iterations=3,
        )

        initial = runner.run(_make_job("请调用 sample.write 写入 report"), defer_permission=True)
        checkpoint = initial[-1].runtime_event["payload"]["request"]["_internal_checkpoint"]
        resumed = runner.resume_permission(checkpoint, "allow_once")

        assert calls == [True]
        assert [event.event_type for event in initial + resumed].count("permission.required") == 1
        failed = next(event for event in resumed if event.event_type == "agent.run.failed")
        assert failed.runtime_event["payload"]["error"]["code"] == ("TARGET_CONTAINER_REQUIRED")

    def test_failed_external_search_cannot_fall_back_to_local_rag(self):
        rag_calls = []
        registry = ToolRegistry()
        registry.register(
            ToolManifest(
                name="rag.search",
                risk_level_default="L0",
                metadata={
                    "loop": {
                        "operation": "retrieve_evidence",
                        "evidence_domain": "workspace.indexed_documents",
                        "substitutable_evidence_domains": [],
                    }
                },
            ),
            lambda _request: rag_calls.append(True) or ToolResult(ok=True),
        )
        registry.register(
            ToolManifest(
                name="literature.search_arxiv",
                risk_level_default="L0",
                metadata={
                    "loop": {
                        "operation": "retrieve_evidence",
                        "evidence_domain": "external_literature.arxiv",
                        "substitutable_evidence_domains": [],
                    }
                },
            ),
            lambda _request: ToolResult(ok=True),
        )
        state = AgentState(
            task_id="task-1",
            run_id="run-1",
            user_goal="继续处理已有外部文献检索结果",
            observations=[
                {
                    "tool_call_id": "arxiv-call",
                    "tool_name": "literature.search_arxiv",
                    "model_action": {
                        "action_type": "call_tool",
                        "tool_name": "literature.search_arxiv",
                        "arguments": {"query": "agent runtime", "max_results": 3},
                        "reason": "检索外部文献",
                    },
                    "ok": False,
                    "summary": "arXiv 请求超时，已完成有限次数重试",
                    "error": {
                        "code": "ARXIV_SEARCH_TIMEOUT",
                        "message": "arXiv 请求超时，已完成有限次数重试",
                        "category": "tool",
                        "recoverable": True,
                    },
                }
            ],
            iteration=1,
        )
        runner = AgentRunner(
            model_provider=_FixedActionModelProvider(
                [
                    AgentAction.call_tool("rag.search", {"query": "agent runtime"}),
                    AgentAction.finish(
                        "外部文献来源当前不可用，未使用本地资料替代。",
                        insufficient_evidence=True,
                    ),
                ]
            ),
            tool_gateway=ToolGateway(registry, PermissionManager()),
            max_iterations=4,
        )

        events = runner.run(
            _make_job(state.user_goal),
            _initial_state=state,
        )

        assert rag_calls == []
        rejected = next(
            event
            for event in events
            if event.event_type == "model.call.failed"
            and event.runtime_event["payload"]["error_code"] == "SEMANTIC_SOURCE_SUBSTITUTION"
        )
        assert rejected.runtime_event["payload"]["recoverable"] is True
        assert not any(event.event_type == "tool.call.finished" for event in events)

    def test_successful_write_effect_is_not_overridden_by_retrieval_advice(self):
        registry = ToolRegistry()
        registry.register(
            ToolManifest(name="rag.search", risk_level_default="L0"),
            lambda _request: ToolResult(ok=True, kind="json", data={"results": []}),
        )
        registry.register(
            ToolManifest(name="knowledge.create_document", risk_level_default="L1"),
            lambda _request: ToolResult(
                ok=True,
                kind="json",
                summary="笔记已保存",
                data={"document_id": str(uuid4())},
            ),
        )
        runner = AgentRunner(
            model_provider=_FixedActionModelProvider(
                [
                    AgentAction.call_tool(
                        "knowledge.create_document",
                        {"title": "边界思考", "content": "RAG 服务模型检索。"},
                    ),
                    AgentAction.finish("笔记已保存"),
                ]
            ),
            tool_gateway=ToolGateway(registry, PermissionManager()),
            worker_id="test",
            max_iterations=3,
        )

        events = runner.run(
            _make_job("帮我保存一段想法：RAG 服务模型检索，以后复盘架构时还想看到。")
        )

        assert events[-1].event_type == "agent.run.completed"
        assert not any(
            event.runtime_event["payload"].get("error_code") == "REQUIRED_TOOL_EVIDENCE_MISSING"
            for event in events
        )

    def test_implicit_knowledge_intent_requires_rag_without_database_wording(self):
        model = _FixedActionModelProvider(
            [
                AgentAction.finish("直接回答"),
                AgentAction.call_tool(
                    "rag.search",
                    {"query": "QLoRA 降低显存的原理"},
                    "检索当前 Workspace 的专业文档",
                ),
                AgentAction.finish("当前文档库没有足够证据", insufficient_evidence=True),
            ]
        )
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_rag_tool_gateway(),
            worker_id="test",
            max_iterations=4,
        )

        envelopes = runner.run(_make_job("QLoRA 为什么能够降低训练显存？"))

        assert [event.event_type for event in envelopes].count("model.call.failed") == 1
        assert "tool.call.finished" in [event.event_type for event in envelopes]
        completed = next(event for event in envelopes if event.event_type == "agent.run.completed")
        assert completed.runtime_event["payload"]["output"] == "当前文档库没有足够证据"

    def test_retrieval_intent_buffers_first_model_output(self):
        model = _StreamingFinishModelProvider("不应提前展示的直接回答")
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_rag_tool_gateway(),
            worker_id="test",
            max_iterations=1,
        )

        envelopes = runner.run(_make_job("解释 QLoRA 的工作原理"))

        assert "model.delta" not in [event.event_type for event in envelopes]
        failed = next(event for event in envelopes if event.event_type == "agent.run.failed")
        assert failed.runtime_event["payload"]["error"]["code"] == ("REQUIRED_TOOL_NOT_EXECUTED")

    def test_explanatory_tool_question_can_finish_without_execution(self):
        model = _FixedActionModelProvider(
            AgentAction.finish("这是一个在 workspace 内创建新文件的工具。")
        )
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=ToolGateway(create_tool_registry(), PermissionManager()),
            worker_id="test",
        )
        job = _make_job("如何使用 workspace.create_file？请只解释，不要执行。")

        envelopes = runner.run(job)

        event_types = [event.event_type for event in envelopes]
        assert "agent.run.completed" in event_types
        assert "model.call.failed" not in event_types
        assert "tool.call.started" not in event_types


class TestRagFinalAnswerValidation:
    def test_validator_can_buffer_stream_until_final_answer_is_validated(self):
        class TrackingModelProvider(_StreamingFinishModelProvider):
            def __init__(self, message):
                super().__init__(message)
                self.non_stream_calls = 0
                self.stream_calls = 0

            def decide_next_action(self, state):
                self.non_stream_calls += 1
                return super().decide_next_action(state)

            def decide_next_action_stream(self, state, on_text_delta):
                self.stream_calls += 1
                return super().decide_next_action_stream(state, on_text_delta)

        class BufferingValidator:
            validator_id = "buffering-test"

            def requires_buffered_output(self, _state):
                return True

            def validate(self, *, action, state):
                return FinalAnswerValidation(
                    accepted=True,
                    output=action.final_message,
                )

        model = TrackingModelProvider("需要先校验的回答")
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            final_answer_validators=(BufferingValidator(),),
        )

        envelopes = runner.run(_make_job("回答问题"))

        assert "model.delta" not in [event.event_type for event in envelopes]
        assert model.non_stream_calls == 1
        assert model.stream_calls == 0
        completed = next(event for event in envelopes if event.event_type == "agent.run.completed")
        assert completed.runtime_event["payload"]["output"] == "需要先校验的回答"

    def test_approved_language_preference_buffers_and_rewrites_invalid_answer(self):
        model = _FixedActionModelProvider(
            [
                AgentAction.finish(
                    "A runtime checkpoint stores resumable state. It supports recovery after a crash."
                ),
                AgentAction.finish(
                    "Runtime checkpoint 会保存可恢复的执行状态。它让任务能在进程崩溃后继续运行。"
                ),
            ]
        )
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
        )

        envelopes = runner.run(
            _make_job("Explain what a runtime checkpoint is in two sentences."),
            memory_items=[
                {
                    "scope_type": "global",
                    "category": "preference",
                    "key": "response.language",
                    "content": "用户偏好使用中文回答，技术名词可以保留英文。",
                }
            ],
        )

        event_types = [event.event_type for event in envelopes]
        assert "model.delta" not in event_types
        assert event_types.count("model.call.failed") == 1
        failed = next(event for event in envelopes if event.event_type == "model.call.failed")
        validation = failed.runtime_event["payload"]["validation"]
        assert validation["validator_id"] == "response-language-preference-v1"
        assert validation["reason_code"] == "RESPONSE_LANGUAGE_MISMATCH"
        completed = next(event for event in envelopes if event.event_type == "agent.run.completed")
        assert completed.runtime_event["payload"]["output"].startswith("Runtime checkpoint 会保存")
        metadata = completed.runtime_event["payload"]["answer_validation"][
            "response-language-preference-v1"
        ]
        assert metadata["effective_language"] == "zh"
        assert metadata["current_turn_override"] is False

    def test_explicit_current_turn_language_overrides_approved_default(self):
        model = _FixedActionModelProvider(
            AgentAction.finish(
                "A runtime checkpoint stores resumable state and enables crash recovery."
            )
        )
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
        )

        envelopes = runner.run(
            _make_job("Please answer in English. Explain a runtime checkpoint."),
            memory_items=[
                {
                    "scope_type": "global",
                    "category": "preference",
                    "key": "response.language",
                    "content": "默认使用中文回答。",
                }
            ],
        )

        assert "model.call.failed" not in [event.event_type for event in envelopes]
        completed = next(event for event in envelopes if event.event_type == "agent.run.completed")
        metadata = completed.runtime_event["payload"]["answer_validation"][
            "response-language-preference-v1"
        ]
        assert metadata["effective_language"] == "en"
        assert metadata["current_turn_override"] is True

    def test_invalid_citation_retries_then_persists_trusted_citation(self):
        chunk_id, document_id, artifact_id = uuid4(), uuid4(), uuid4()
        observation = {
            "tool_call_id": "rag-call",
            "tool_name": "rag.search",
            "model_action": {
                "action_type": "call_tool",
                "tool_name": "rag.search",
                "arguments": {"query": "question"},
                "reason": "检索证据",
            },
            "ok": True,
            "summary": "检索完成",
            "data": {
                "results": [
                    {
                        "chunk_id": str(chunk_id),
                        "document_id": str(document_id),
                        "document_title": "Trusted paper",
                        "source_artifact_id": str(artifact_id),
                        "chunks": [
                            {
                                "chunk_id": str(chunk_id),
                                "role": "primary",
                                "content": "trusted evidence",
                                "source_locator": {"page_start": 9},
                            }
                        ],
                        "elements": [],
                    }
                ],
                "evidence_assessment": {
                    "schema": RAG_EVIDENCE_ASSESSMENT_SCHEMA,
                    "policy_version": RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
                    "sufficient": True,
                    "reason_code": "SUFFICIENT",
                },
            },
        }
        state = AgentState(
            task_id="task-1",
            run_id="run-1",
            user_goal="回答问题",
            observations=[observation],
            iteration=1,
        )
        model = _FixedActionModelProvider(
            [
                AgentAction.finish("第一次回答", citations=({"chunk_id": str(uuid4())},)),
                AgentAction.finish("可信回答", citations=({"chunk_id": str(chunk_id)},)),
            ]
        )
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            final_answer_validators=(RagCitationValidator(),),
            max_iterations=4,
        )

        envelopes = runner.run(_make_job("回答问题"), _initial_state=state)

        assert [event.event_type for event in envelopes].count("model.call.failed") == 1
        completed = next(event for event in envelopes if event.event_type == "agent.run.completed")
        output = completed.runtime_event["payload"]["output"]
        assert "可信回答" in output
        assert "Trusted paper" in output
        assert "p.9" in output
        validation = completed.runtime_event["payload"]["answer_validation"]
        assert validation["rag-citation-v1"]["citations"][0]["chunk_id"] == str(chunk_id)

    def test_model_authored_citation_section_is_replaced_without_retry(self):
        chunk_id, document_id, artifact_id = uuid4(), uuid4(), uuid4()
        observation = {
            "tool_call_id": "rag-call",
            "tool_name": "rag.search",
            "model_action": {
                "action_type": "call_tool",
                "tool_name": "rag.search",
                "arguments": {"query": "question"},
                "reason": "检索证据",
            },
            "ok": True,
            "summary": "检索完成",
            "data": {
                "results": [
                    {
                        "chunk_id": str(chunk_id),
                        "document_id": str(document_id),
                        "document_title": "Trusted paper",
                        "source_artifact_id": str(artifact_id),
                        "chunks": [
                            {
                                "chunk_id": str(chunk_id),
                                "role": "primary",
                                "content": "trusted evidence",
                                "source_locator": {"page_start": 9},
                            }
                        ],
                        "elements": [],
                    }
                ],
                "evidence_assessment": {
                    "schema": RAG_EVIDENCE_ASSESSMENT_SCHEMA,
                    "policy_version": RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
                    "sufficient": True,
                    "reason_code": "SUFFICIENT",
                },
            },
        }
        state = AgentState(
            task_id="task-1",
            run_id="run-1",
            user_goal="回答问题",
            observations=[observation],
            iteration=1,
        )
        model = _FixedActionModelProvider(
            [
                AgentAction.finish(
                    f"正文。\n\n引用：\n- [1] Trusted paper · p.9 (`chunk:{chunk_id}`)",
                    citations=({"chunk_id": str(chunk_id)},),
                ),
                AgentAction.finish("正文。", citations=({"chunk_id": str(chunk_id)},)),
            ]
        )
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            final_answer_validators=(RagCitationValidator(),),
            max_iterations=4,
        )

        envelopes = runner.run(_make_job("回答问题"), _initial_state=state)

        assert [event.event_type for event in envelopes].count("model.call.failed") == 0
        completed = next(event for event in envelopes if event.event_type == "agent.run.completed")
        output = completed.runtime_event["payload"]["output"]
        assert output.startswith("正文。\n\n引用：\n")
        assert output.count("引用：") == 1
        assert output.count(f"chunk_id={chunk_id}") == 1
        assert output.count("[引用 1]") == 1

    def test_answer_validators_compose_host_clamp_before_rag_rendering(self):
        chunk_id, document_id, artifact_id = uuid4(), uuid4(), uuid4()
        observation = {
            "tool_call_id": "rag-call",
            "tool_name": "rag.search",
            "model_action": {
                "action_type": "call_tool",
                "tool_name": "rag.search",
                "arguments": {"query": "verification validation"},
                "reason": "检索证据",
            },
            "ok": True,
            "summary": "检索完成",
            "data": {
                "results": [
                    {
                        "chunk_id": str(chunk_id),
                        "document_id": str(document_id),
                        "document_title": "NIST AI RMF",
                        "source_artifact_id": str(artifact_id),
                        "chunks": [
                            {
                                "chunk_id": str(chunk_id),
                                "role": "primary",
                                "content": "trusted evidence",
                                "source_locator": {"page_start": 11},
                            }
                        ],
                        "elements": [],
                    }
                ],
                "evidence_assessment": {
                    "schema": RAG_EVIDENCE_ASSESSMENT_SCHEMA,
                    "policy_version": RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
                    "sufficient": True,
                    "reason_code": "SUFFICIENT",
                },
            },
        }
        state = AgentState(
            task_id="task-1",
            run_id="run-1",
            user_goal="找出所有相关章节，并给出不超过 80 字的总结。",
            observations=[observation],
            iteration=1,
            answer_guard_rejections=1,
        )
        model = _FixedActionModelProvider(
            AgentAction.finish(
                "章节一。" * 80,
                citations=({"chunk_id": str(chunk_id)},),
            )
        )
        runner = AgentRunner(
            model_provider=model,
            tool_gateway=_make_tool_gateway(),
            final_answer_validators=(RagCitationValidator(),),
            max_iterations=4,
        )

        envelopes = runner.run(
            _make_job("找出所有相关章节，并给出不超过 80 字的总结。"),
            _initial_state=state,
        )

        assert "model.call.failed" not in [event.event_type for event in envelopes]
        completed = next(event for event in envelopes if event.event_type == "agent.run.completed")
        output = completed.runtime_event["payload"]["output"]
        answer_body = output.split("\n\n引用：", 1)[0]
        assert len("".join(answer_body.split())) <= 80
        assert "本次有界检索" in answer_body
        assert "仍可能遗漏" in answer_body
        assert f"chunk_id={chunk_id}" in output
        validations = completed.runtime_event["payload"]["answer_validation"]
        assert validations["explicit-answer-constraints-v1"]["host_normalized"] is True
        assert validations["rag-citation-v1"]["citations"][0]["chunk_id"] == str(chunk_id)


class TestRunCheckpointRecovery:
    def test_model_boundary_checkpoint_resumes_without_duplicate_run_start(self):
        class SimulatedCrash(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            model = _FixedActionModelProvider(
                [
                    AgentAction.call_tool("workspace.list_files", {"path": "."}, "读取工作区"),
                    AgentAction.finish("恢复后完成"),
                ]
            )
            runner = AgentRunner(model, _make_tool_gateway(), worker_id="test")
            captured: dict[str, Any] = {}

            def crash_after_model_checkpoint(envelope):
                if envelope.event_type == "model.call.completed":
                    captured.update(envelope.internal["run_checkpoint"])
                    raise SimulatedCrash("worker crashed after model checkpoint")

            with pytest.raises(SimulatedCrash):
                runner.run(
                    _make_job("列出文件", workspace_path=tmpdir),
                    publish_cb=crash_after_model_checkpoint,
                )

            assert captured["resume_node"] == "execute_tool"
            assert captured["version"] == RUN_CHECKPOINT_VERSION
            assert is_resumable_run_checkpoint(captured)
            legacy = dict(captured)
            legacy["version"] = 1
            assert not is_resumable_run_checkpoint(legacy)
            with pytest.raises(ValueError, match="checkpoint version"):
                runner.resume_from_checkpoint(legacy)
            resumed = runner.resume_from_checkpoint(captured)
            types = [event.event_type for event in resumed]
            assert "agent.run.started" not in types
            assert types.count("tool.call.started") == 1
            assert types.count("tool.call.finished") == 1
            assert types[-1] == "agent.run.completed"

    def test_internal_checkpoint_never_serializes_with_runtime_event(self):
        published = []
        runner = AgentRunner(
            _FixedActionModelProvider(AgentAction.finish("完成")),
            _make_tool_gateway(),
        )
        runner.run(_make_job("hello"), publish_cb=published.append)
        started = next(event for event in published if event.event_type == "model.call.started")
        assert started.internal["run_checkpoint"]["resume_node"] == "call_model"
        serialized = started.to_payload_json()
        assert "run_checkpoint" not in serialized
        assert "_internal" not in serialized
