# Personal AI Agent 文档集

本文档集用于沉淀一个本地优先的个人 AI Agent 项目设计。当前开发主线优先做 Vue Web 端 Agent 控制台，桌面端在系统稳定完整后再封装和迁移。项目目标不是做一个普通聊天机器人，而是构建一个 Agent Runtime Harness：Vue Web 负责用户观察和接管，Go Gateway / Runtime Orchestrator 负责前端契约、校验、并发调度和事件扇出，Redis Runtime Bus 负责跨进程任务、命令、事件和心跳通信，Python Agent Worker Pool 负责 LangChain / LangGraph、工具、权限、存储、审计和 multi-agent 协作。

## 文档边界

每份文档只负责自己的内容，避免混写：

| 文档 | 负责内容 | 不负责内容 |
| --- | --- | --- |
| [01-project-overview.md](./01-project-overview.md) | 项目定位、目标、原则、MVP 范围 | 具体技术选型、模块实现 |
| [02-system-architecture.md](./02-system-architecture.md) | 总体架构、模块关系、系统运行流程 | 具体 UI 细节、数据库 schema |
| [03-technology-stack.md](./03-technology-stack.md) | 技术栈选择、阶段性选型、取舍原因 | 业务流程和模块职责 |
| [04-desktop-app-design.md](./04-desktop-app-design.md) | 后续 macOS App 形态、窗口、IPC、桌面能力 | 当前 Web-first MVP、Agent 内部推理循环 |
| [05-agent-runtime-design.md](./05-agent-runtime-design.md) | Agent Runtime 核心模块、运行循环、任务状态 | 具体前端布局 |
| [06-multi-agent-design.md](./06-multi-agent-design.md) | 多 Agent 编排、角色、任务图、协作边界 | 单 Agent 工具实现细节 |
| [07-context-memory-design.md](./07-context-memory-design.md) | 上下文、记忆、检索、压缩、注入策略 | 模型供应商选择 |
| [08-permission-security-design.md](./08-permission-security-design.md) | 权限、风险分级、确认机制、审计 | UI 视觉风格 |
| [09-development-guide.md](./09-development-guide.md) | 工程分层、开发方式、模块交付内容、数据流说明模板 | 产品愿景和长期路线 |
| [10-roadmap.md](./10-roadmap.md) | 分阶段计划、MVP、后续演进 | 具体接口定义 |
| [11-frontend-app-ui-design.md](./11-frontend-app-ui-design.md) | Web 前端信息架构、页面布局、组件状态、视觉原则 | Agent Runtime 内部实现 |
| [13-interface-contract.md](./13-interface-contract.md) | Web API / IPC、Runtime events、DTO、错误结构、mock 场景 | 数据库 schema 和 MCP 协议细节 |
| [14-data-schema.md](./14-data-schema.md) | Storage model、关系型 schema 契约、状态约束、恢复流程、migration 策略 | UI 视觉和 Web API / IPC DTO 细节 |
| [15-mcp-tool-gateway-design.md](./15-mcp-tool-gateway-design.md) | MCP 与 ToolGateway 的关系、工具 manifest、权限路径、MCP 生命周期 | 具体 MCP SDK 实现 |
| [16-dev-runtime-runbook.md](./16-dev-runtime-runbook.md) | `scripts/dev.sh setup/start` 一键安装与 Conda 启动、PostgreSQL + Redis + Control Plane + Gateway + Worker + Web 排障和冒烟验收 | 新架构职责、Storage schema、真实 LLM / ToolGateway 行为 |
| [17-python-backend-architecture.md](./17-python-backend-architecture.md) | Python 后端目录结构、模块职责、依赖方向、热路径 / scaffold 状态 | 具体实现细节、非 Python 侧架构 |
| [18-observability-logging-design.md](./18-observability-logging-design.md) | 应用日志系统：格式、脱敏、文件滚动、配置、故障降级、与 RuntimeEvent/AuditLog 的边界 | Web 日志页面、Redis 日志汇聚、数据库日志表 |
| [19-personal-knowledge-base-design.md](./19-personal-knowledge-base-design.md) | Obsidian 个人知识库、独立 Vault、Markdown 与 RAG 边界 | 向量库选型、Embedding 与检索算法 |
| [20-mvp-rc1-release-gate.md](./20-mvp-rc1-release-gate.md) | MVP RC1 自动化门禁、真实用户旅程、证据契约和阻断标准 | 新功能设计、长期 Roadmap |
| [21-p2-reliability-security-hardening.md](./21-p2-reliability-security-hardening.md) | RC1 后 Phase 8 可靠性、安全、恢复、容量与故障注入门禁 | Multi-Agent、桌面封装和普通功能扩展 |
| [22-p3-product-experience-closure.md](./22-p3-product-experience-closure.md) | P2 后 Web 控制台的状态、恢复、权限、信息层级、窄窗口与 RAG 运维体验门禁 | 桌面封装、Multi-Agent 和新的 Runtime 能力 |
| [23-p4-single-agent-knowledge-quality.md](./23-p4-single-agent-knowledge-quality.md) | P3 后单 Agent 多文档研究、RAG 质量基线和知识写入闭环 | 桌面封装、Multi-Agent、插件市场和无评测依据的算法堆叠 |
| [24-p4-rag-quality-baseline.md](./24-p4-rag-quality-baseline.md) | P4 当前生产 RAG 指标、失败归因、门禁与复现证据 | 长期路线和原始生产内容 |
| [25-p4-rag-data-flywheel.md](./25-p4-rag-data-flywheel.md) | P4-3 自动采样、人工复核、晋升、版本对比和发布门禁闭环 | 自动生成金标、后台训练和 Multi-Agent |
| [26-p6-agent-runtime-framework-consolidation.md](./26-p6-agent-runtime-framework-consolidation.md) | P6 LangChain 适配、LangGraph owner 收口、恢复对账与迁移门禁 | RAG 算法优化、Multi-Agent、桌面封装 |
| [27-p7-engineering-release-productization.md](./27-p7-engineering-release-productization.md) | P7 RC2 工程门禁、结构化报告、CI、升级恢复与运维产品化顺序 | 桌面封装、Multi-Agent、无证据的 RAG 算法优化 |
| [28-real-world-usage-evaluation.md](./28-real-world-usage-evaluation.md) | 真实用户任务评测协议、版本化场景集、评分、证据、缺陷归因和候选阻断规则 | 单元测试实现、mock 场景和具体业务能力设计 |

## 推荐更新方式

后续讨论时按主题更新：

- 项目定位变化：更新 `01-project-overview.md`
- 架构或模块边界变化：更新 `02-system-architecture.md`
- 技术选型变化：更新 `03-technology-stack.md`
- 后续 Mac App / Electron 体验变化：更新 `04-desktop-app-design.md`
- Agent Runtime 机制变化：更新 `05-agent-runtime-design.md`
- multi-agent 机制变化：更新 `06-multi-agent-design.md`
- memory/context 机制变化：更新 `07-context-memory-design.md`
- 权限与安全规则变化：更新 `08-permission-security-design.md`
- 开发流程、模块交付规范变化：更新 `09-development-guide.md`
- 阶段计划变化：更新 `10-roadmap.md`
- Web 前端页面、交互和视觉策略变化：更新 `11-frontend-app-ui-design.md`
- Web API / IPC、事件、DTO、错误结构变化：更新 `13-interface-contract.md`
- 数据表、状态枚举、migration、恢复策略变化：更新 `14-data-schema.md`
- MCP、ToolGateway、工具 manifest、工具权限路径变化：更新 `15-mcp-tool-gateway-design.md`
- 本地开发启动、runtime 冒烟验收、常见运行失败判断变化：更新 `16-dev-runtime-runbook.md`
- Python 后端目录结构、模块迁移、bootstrap 组装逻辑变化：更新 `17-python-backend-architecture.md`
- Obsidian Vault、Markdown 索引或个人知识库与 RAG 边界变化：更新 `19-personal-knowledge-base-design.md`
- MVP RC1 门禁、用户旅程、发布证据或阻断标准变化：更新 `20-mvp-rc1-release-gate.md`
- P2 可靠性、安全加固、故障注入、备份恢复和容量门禁变化：更新 `21-p2-reliability-security-hardening.md`
- P3 Web 产品体验、恢复交互、信息层级、响应式布局和 RAG 运维变化：更新 `22-p3-product-experience-closure.md`
- P4 多文档研究、RAG 质量基线和单 Agent 知识闭环变化：更新 `23-p4-single-agent-knowledge-quality.md`
- P4 当前版本生产 RAG 指标、门禁、失败归因与复现证据：更新 `24-p4-rag-quality-baseline.md`
- P4 数据飞轮自动采样、审核队列、晋升和发布门禁变化：更新 `25-p4-rag-data-flywheel.md`
- P6 LangChain/LangGraph 当前基线、迁移顺序和框架边界变化：更新 `26-p6-agent-runtime-framework-consolidation.md`
- P7 RC2 发布门禁、CI、首次启动、升级恢复或运维产品化变化：更新 `27-p7-engineering-release-productization.md`
- 真实用户任务集、使用评测协议、评分和问题归因变化：更新 `28-real-world-usage-evaluation.md`
