# Jarvis Web Console

Vue 3 + TypeScript + Vite 实现的本地 Agent 控制台。

主要界面包括 Command Center、任务列表、运行时间线、权限接管、工具调用检查器、Memory、Knowledge、RAG 文档管理、Audit、Runtime 和 Settings。

Web 层只消费 Gateway 提供的 DTO 与 Runtime Event，不直接访问数据库、本地文件、Shell、模型供应商或 Redis。

## Commands

```bash
npm ci
npm run dev
npm test
npm run build
```

默认开发地址为 <http://127.0.0.1:5173>，API 请求通过 Vite 配置转发到本地 Gateway。
