"""单 Agent LangGraph 节点与路由规则。

节点只控制图的流向。模型调用、动作校验、工具执行、权限、审计与 RuntimeEvent
的语义继续由注入的 AgentRunner 执行器拥有，因而不能绕过 ToolGateway 或
PermissionManager。
"""

from __future__ import annotations

from typing import Callable, Literal

from jarvis_worker.agent.core.graph_state import AgentGraphState, AgentGraphUpdate
from jarvis_worker.agent.harness import RunSupervisor
from jarvis_worker.runtime_bus.messages import RuntimeEventEnvelope

InitializeExecutor = Callable[[AgentGraphState], AgentGraphUpdate]
TurnExecutor = Callable[[AgentGraphState], AgentGraphUpdate]
MaxIterationFailureFactory = Callable[[AgentGraphState], RuntimeEventEnvelope]

class AgentGraphNodes:
    """绑定 Runtime 执行器后的 LangGraph nodes。"""

    def __init__(
        self,
        *,
        initialize_run: InitializeExecutor,
        extract_intent: TurnExecutor,
        call_model: TurnExecutor,
        validate_action: TurnExecutor,
        execute_tool: TurnExecutor,
        observe_result: TurnExecutor,
        build_max_iterations_failure: MaxIterationFailureFactory,
        max_iterations: int,
    ) -> None:
        self._initialize_run = initialize_run
        self._extract_intent = extract_intent
        self._call_model = call_model
        self._validate_action = validate_action
        self._execute_tool = execute_tool
        self._observe_result = observe_result
        self._build_max_iterations_failure = build_max_iterations_failure
        self._max_iterations = max_iterations

    def initialize_run(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """发布运行起点，或在已取消时直接进入终态。"""
        return self._initialize_run(graph_state)

    def route_from_start(
        self, graph_state: AgentGraphState
    ) -> Literal[
        "initialize_run", "extract_intent", "call_model", "validate_action", "execute_tool"
    ]:
        resume_node = graph_state.get("resume_node", "initialize_run")
        if resume_node in (
            "extract_intent",
            "call_model",
            "validate_action",
            "execute_tool",
        ):
            return resume_node
        return "initialize_run"

    def extract_intent(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """调用并校验 LLM Intent；不执行工具或副作用。"""
        return self._extract_intent(graph_state)

    def call_model(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """调用模型，并把未受信任的动作保留在图内临时状态。"""
        return self._call_model(graph_state)

    def validate_action(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """校验模型动作与 effect guard；只有通过后才允许执行工具。"""
        return self._validate_action(graph_state)

    def execute_tool(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """经 ToolGateway 与 PermissionManager 执行已验证的工具请求。"""
        return self._execute_tool(graph_state)

    def observe_result(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """把 ToolResult 投影为事件和 AgentState observation。"""
        return self._observe_result(graph_state)

    def route_after_initialize(
        self, graph_state: AgentGraphState
    ) -> Literal["extract_intent", "call_model", "end"]:
        envelopes = graph_state["envelopes"]
        if envelopes and RunSupervisor.is_stop_event(envelopes[-1].event_type):
            return "end"
        return "call_model" if graph_state["state"].intent is not None else "extract_intent"

    def route_after_intent(
        self, graph_state: AgentGraphState
    ) -> Literal["extract_intent", "call_model", "end"]:
        envelopes = graph_state["envelopes"]
        if envelopes and RunSupervisor.is_stop_event(envelopes[-1].event_type):
            return "end"
        if graph_state["turn"].get("retry_intent"):
            return "extract_intent"
        return "call_model" if graph_state["state"].intent is not None else "end"

    def route_after_model(
        self, graph_state: AgentGraphState
    ) -> Literal["call_model", "validate_action", "end"]:
        """模型调用失败、暂停或取消后不得继续消费不存在的 action。"""
        envelopes = graph_state["envelopes"]
        if envelopes and RunSupervisor.is_stop_event(envelopes[-1].event_type):
            return "end"
        if graph_state["turn"].get("retry_model"):
            return "call_model"
        return "validate_action"

    def route_after_validation(
        self, graph_state: AgentGraphState
    ) -> Literal["call_model", "execute_tool", "end"]:
        """动作校验失败、finish 和 effect guard 不会流入工具执行节点。"""
        envelopes = graph_state["envelopes"]
        if envelopes and RunSupervisor.is_stop_event(envelopes[-1].event_type):
            return "end"
        turn = graph_state["turn"]
        if turn.get("retry_model"):
            return "call_model"
        if turn.get("tool_request") is None:
            return "end"
        return "execute_tool"

    def route_after_tool_execution(
        self, graph_state: AgentGraphState
    ) -> Literal["observe_result", "end"]:
        envelopes = graph_state["envelopes"]
        if envelopes and RunSupervisor.is_stop_event(envelopes[-1].event_type):
            return "end"
        if graph_state["turn"].get("tool_result") is None:
            return "end"
        return "observe_result"

    def route_after_observation(
        self, graph_state: AgentGraphState
    ) -> Literal["call_model", "max_iterations", "end"]:
        """工具预算耗尽后仍保留一次只允许 finish 的证据收口机会。"""
        envelopes = graph_state["envelopes"]
        if not envelopes or RunSupervisor.is_stop_event(envelopes[-1].event_type):
            return "end"
        return "call_model"

    def max_iterations(self, graph_state: AgentGraphState) -> AgentGraphUpdate:
        """以既有 AppError/RuntimeEvent 契约生成最大迭代终态。"""
        failure = self._build_max_iterations_failure(graph_state)
        return {"envelopes": [*graph_state["envelopes"], failure]}
