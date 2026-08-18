# 项目总览

## 项目定位

本项目是一个本地优先的个人 AI Agent 控制台，类似无实体版本的 Jarvis。当前开发主线优先做 Web 端 Agent 控制台，用浏览器界面跑通产品交互、Agent Runtime、工具权限、任务状态和可观察闭环；桌面端版本在系统稳定完整之后再做封装和迁移。

当前工程分层采用：

```text
Vue Web = 用户观察、接管和控制台
Go Gateway / Runtime Orchestrator = 前端契约守门人、校验、错误归一、并发调度、worker 治理和事件扇出
Redis Runtime Bus = run queue、worker command、runtime event、heartbeat 和短期协调状态
Python Agent Worker Pool = Agent Runtime 大脑、LangChain / LangGraph、工具、权限、存储和审计
```

本项目采用 Agent Harness Engineering 和 Loop Engineering 作为核心开发思想。Harness 在这里不是某个固定框架，也不一定对应一个单独命名的代码模块，而是指让 LLM 能够在个人电脑上安全、持续、可观察地执行任务的一整套运行系统。

```text
LLM = 推理组件
LangChain = 模型、prompt、tool wrapper、retriever、parser 等能力组件
LangGraph = Agent loop / graph 编排组件
Harness = 运行环境、工具边界、上下文、记忆、权限、状态、审计和可观测性
Agent = LLM + Harness
```

这里的 Harness 不是为了削弱 Agent 的自主性，而是为了给自主性建立边界。Agent 可以自主理解目标、规划步骤、选择工具、生成工具调用参数，并根据执行结果继续推理；但所有会影响本地电脑、外部服务或敏感数据的真实动作，都必须经过 Harness 的 ToolGateway、PermissionManager、AuditLog 和 Storage。LangChain / LangGraph 可以帮助实现能力和编排，但不能替代项目自己的权限、工具、存储、事件和接口契约。

它应该具备以下能力：

- 接收用户自然语言任务。
- 使用 LLM 进行理解、规划、推理和总结。
- 允许 Agent 在受控范围内自主决策、选择工具并发起动作。
- 在个人电脑上调用本地能力，例如文件、Shell、浏览器、剪贴板、通知等。
- 对简单任务使用 single-agent 执行。
- 对复杂任务使用 multi-agent 和 task graph 协作。
- 管理任务状态、上下文、记忆、权限和执行日志。
- 在危险操作前请求用户确认。

## 核心判断

项目的核心不是 skill，也不是单个 prompt，也不是 LangChain / LangGraph 框架本身。Skill、工具、MCP server、LangChain 组件、本地能力都只是 Harness 可以调度的能力。

真正核心是：

```text
LLM + Python Agent Worker Runtime Harness + LangGraph Agent Loop + LangChain 能力组件 + 本地电脑能力 + MCP / Tool Gateway + 上下文管理 + 记忆 + 权限 + 任务状态管理 + 多 Agent 编排 + Go Runtime Orchestrator + Redis Runtime Bus + Vue Web 控制台
```

也就是说，项目要优先解决“Agent 如何在个人电脑上稳定运行”，而不是先堆很多工具。

## 产品形态

当前阶段产品形态是 Web 端个人 Agent 控制台：

- 有 Web 主界面和 Command Center。
- 可以通过 Go Gateway / Runtime Orchestrator 和 Redis Runtime Bus 连接 Python Agent Worker Runtime。
- 可以连接云端 LLM，后续支持本地 LLM。
- 可以保存任务历史、运行日志、权限和配置。
- 可以展示 Agent 的执行过程。
- 可以在 Web 界面中展示权限确认和接管入口。

Web 优先不等于普通远端聊天网页。当前 Web App 是为了快速验证完整 Agent Runtime Harness、交互结构和可观察流程。桌面端不是第一闭环目标；等 Web 端交互、Runtime、权限、安全、存储和工具系统稳定完整后，再将稳定的 UI 和 Runtime 能力封装进 macOS 桌面 App。

## 设计原则

### 本地优先

任务、日志、设置、记忆优先保存在本地。云端 LLM 可以作为模型能力来源，但不应该让所有系统状态依赖云端服务。

### Runtime 优先

先做稳定的 Agent Runtime，再扩展更多工具和复杂 UI。

### Loop 优先

所有任务都应该通过可观察、可恢复的 Agent loop 执行，而不是一次性模型调用。简单任务使用 single-agent loop，复杂任务使用 multi-agent task graph 编排多个受控 loop。

### 透明可观察

用户应该知道 Agent 当前在做什么、调用了什么工具、为什么需要权限、任务做到哪一步。

### 可控

Agent 可以自主发起工具调用和本地动作请求，但不能绕过 Harness 直接操作电脑。涉及文件写入、Shell、邮件、删除、购买、系统设置等操作时，需要权限分级；低风险动作可自动执行，中高风险动作需要用户确认，高风险或禁止动作不能被永久自动批准。

### 可恢复

长任务不能因为窗口关闭或模型调用失败而完全丢失。任务状态应持久化。

### Multi-agent 按需启用

默认使用 single-agent。只有任务复杂度、并行度或审查需求达到阈值时，才进入 multi-agent。

## MVP 范围

第一版目标是跑通一个最小但完整的闭环：

```text
用户输入任务
-> Vue Web 通过 Go Gateway 创建任务
-> Go Runtime Orchestrator 入队 AgentRun
-> Python Agent Worker 创建或恢复 Task / AgentRun
-> Runtime 构造上下文
-> Agent 自主规划下一步并发起工具调用
-> Harness 进行工具校验和权限判断
-> 必要时请求用户确认
-> 工具结果返回
-> Agent 继续执行
-> 验证结果
-> 保存任务历史
-> Python Worker 写入 RuntimeEvent
-> Redis Runtime Bus 承载事件
-> Go Gateway 扇出 RuntimeEvent
-> UI 展示最终结果
```

MVP 应包含：

- Web 端 Command Center / Command Chat。
- 任务列表。
- Agent 执行时间线。
- Go Gateway / Runtime Orchestrator。
- Redis Runtime Bus。
- Python mock Agent Worker。
- Dev Console。
- 模型配置。
- 本地持久化存储层。
- Agent Runtime。
- 基础 Context Manager。
- 基础 Tool Gateway。
- 基础 Permission Manager。
- 文件读取工具。
- 低风险 Shell 工具。

MVP 暂不包含：

- 完整语音唤醒。
- 完整长期记忆系统。
- 插件市场。
- 完整 App 自动化。
- macOS 桌面 App 壳。
- 复杂 multi-agent 自由讨论。
