"""Agent Runner — 模型决策 → AgentAction → ToolGateway → observe → finish 的最小循环。

当前职责：
- 定义 AgentAction、AgentState 和 AgentRunner 核心循环。
- `graph_state.py` 拥有图内 TypedDict，`graph_nodes.py` 拥有节点与路由，`graph.py` 拥有 StateGraph 装配。
- 生产意图提取位于 agent.intents；测试场景动作模拟仍位于 tests/testing_doubles.py。
- 通过注入的 ModelProvider 获取结构化 AgentAction。
- 通过 ToolGateway 执行工具，不直接触碰 Redis、DB、OS 或 MCP。

模型 provider 实现在 jarvis_worker.agent.models 包中。
"""
