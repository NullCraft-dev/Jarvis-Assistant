# Repositories

这里保留跨 Runtime 功能共用的 Repository 接口。具体实现跟随对应功能目录放置。

Memory 已拥有独立的 `agent/memory/repository.py`，后续其他功能达到独立演进规模时，
也应将自己的接口迁回功能目录，而不是继续扩大通用接口文件。
