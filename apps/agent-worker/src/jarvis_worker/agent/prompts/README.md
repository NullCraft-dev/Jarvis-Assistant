# Prompts

`prompts/` 负责版本化 prompt 资产。

系统 prompt、工具选择 prompt、文件总结 prompt、结构化输出 prompt 应以文件形式放在这里，避免散落在代码字符串中。

Prompt 只描述模型行为。Runtime 边界仍由 AgentRunner、ToolGateway、Permission、Storage 和 contracts 保证。
