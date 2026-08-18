# Models

`models/` 负责模型层接入和模型路由。

后续 Cloud LLM、本地模型、mock provider、ModelRouter、LangChain wrapper 都应放在这里。ModelProvider 只能通过明确接口返回文本或结构化 AgentAction。

模型层不能执行工具、不能访问 Redis，也不能绕过 AgentRunner 的 action 校验。

供应商能力不得写入通用 OpenAI-compatible adapter。当前
`DeepSeekModelProvider` 独立拥有 `thinking`、JSON Output
`response_format={"type":"json_object"}`，以及在尚未输出安全文本时对
`MODEL_OUTPUT_INVALID` 的一次有界重试；自定义兼容 Provider 不自动发送这些字段。
