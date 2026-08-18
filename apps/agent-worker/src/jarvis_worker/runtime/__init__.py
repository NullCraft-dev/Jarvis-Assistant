"""Agent Runtime — worker loop + mock runner + event builder。

当前 runtime 负责 worker 主循环、mock runner 适配和 RuntimeEvent 构造。
真实工具调用通过 AgentRunner -> ToolGateway -> PermissionManager 进入受控路径。

当前仍不做：
- 真实 LLM / LangGraph
- Storage 持久化
"""
