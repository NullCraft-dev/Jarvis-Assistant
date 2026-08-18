"""单 Agent LangGraph 的图装配 owner。"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from jarvis_worker.agent.core.graph_nodes import AgentGraphNodes
from jarvis_worker.agent.core.graph_state import AgentGraphState


def compile_single_agent_graph(nodes: AgentGraphNodes):
    """编译控制流图，不配置 LangGraph checkpointer。

    Storage、权限恢复与审计仍由项目自身的 PostgreSQL 投影负责。
    """
    graph = StateGraph(AgentGraphState)
    graph.add_node("initialize_run", nodes.initialize_run)
    graph.add_node("extract_intent", nodes.extract_intent)
    graph.add_node("call_model", nodes.call_model)
    graph.add_node("validate_action", nodes.validate_action)
    graph.add_node("execute_tool", nodes.execute_tool)
    graph.add_node("observe_result", nodes.observe_result)
    graph.add_node("max_iterations", nodes.max_iterations)
    graph.add_conditional_edges(
        START,
        nodes.route_from_start,
        {
            "initialize_run": "initialize_run",
            "extract_intent": "extract_intent",
            "call_model": "call_model",
            "validate_action": "validate_action",
            "execute_tool": "execute_tool",
        },
    )
    graph.add_conditional_edges(
        "initialize_run", nodes.route_after_initialize,
        {"extract_intent": "extract_intent", "call_model": "call_model", "end": END},
    )
    graph.add_conditional_edges(
        "extract_intent", nodes.route_after_intent,
        {"extract_intent": "extract_intent", "call_model": "call_model", "end": END},
    )
    graph.add_conditional_edges(
        "call_model", nodes.route_after_model,
        {"call_model": "call_model", "validate_action": "validate_action", "end": END},
    )
    graph.add_conditional_edges(
        "validate_action", nodes.route_after_validation,
        {"call_model": "call_model", "execute_tool": "execute_tool", "end": END},
    )
    graph.add_conditional_edges(
        "execute_tool", nodes.route_after_tool_execution,
        {"observe_result": "observe_result", "end": END},
    )
    graph.add_conditional_edges(
        "observe_result", nodes.route_after_observation,
        {"call_model": "call_model", "max_iterations": "max_iterations", "end": END},
    )
    graph.add_edge("max_iterations", END)
    return graph.compile()
