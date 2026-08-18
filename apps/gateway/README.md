# Jarvis Go Gateway

Go Gateway 是 Web API 入口与 Runtime Orchestrator。它负责请求契约、运行调度、
Redis Runtime Bus 接线、Worker 状态与事件扇出；不执行 Agent loop、模型调用或工具。

```text
cmd/gateway/           进程入口，仅调用 internal/app
internal/app/          依赖装配、路由注册和服务生命周期
internal/api/          HTTP handlers 与 middleware
internal/contracts/    Web API / Runtime 共享契约
internal/orchestrator/ RuntimeBus 抽象、event/heartbeat pump、worker 状态
internal/redis/        Redis Streams 协议与具体 adapter
internal/controlplane/ Python Control Plane 类型化 HTTP client
internal/observability 日志与 trace 支撑
internal/testkit/       测试专用 runtime fixture
```

依赖方向：

```text
cmd/gateway
  -> app
  -> api / orchestrator
  -> controlplane / redis
  -> contracts
```

`api` 不直接访问 PostgreSQL、Redis client、模型或工具；`orchestrator` 不拥有
Task/Run 的持久化真相；`redis` 只实现运行时通信。

每个 Handler 在自己的领域文件中声明最小 Control Plane 接口，并通过构造函数一次性注入。
Handler 不持有完整 Client 能力，也不允许通过 setter 在启动后修改依赖。`internal/app`
中的 `dependencies.go` 负责创建具体实现，`routes.go` 只负责 Handler 与路由装配，
`app.go` 只负责服务和后台 pump 生命周期。

常用命令：

```bash
go run ./cmd/gateway
go test ./...
go vet ./...
```
