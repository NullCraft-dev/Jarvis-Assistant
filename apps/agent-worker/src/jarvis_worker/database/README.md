# Database

`database/` 只负责公共数据库基础设施：连接池、ORM 映射、Unit of Work、事务以及
Outbox/Inbox。当前唯一实现是 PostgreSQL，因此不再额外增加没有信息增量的
`postgres/` 目录。

具体功能的 service 与 PostgreSQL repository 跟随功能放置，例如：

```text
agent/memory/postgres_repository.py
runtime/tasks/postgres_repository.py
runtime/workspaces/postgres_repository.py
```

上层业务仍然依赖 Repository 接口，不能直接操作数据库 client。目录上的功能聚合不
等于取消 Storage Access Layer 抽象。
