from jarvis_worker.agent.tool_gateway.contracts import ToolManifest
from jarvis_worker.agent.tool_gateway.modules import CapabilityModule, ToolBinding


def create_knowledge_capability(executor) -> CapabilityModule:
    manifest = ToolManifest(
        name="knowledge.create_document", provider="native",
        description=(
            "在已连接的 Jarvis Obsidian Vault 中创建报告、笔记或来源说明；"
            "Knowledge Service 会在写入内自动校验标题、正文、标签、来源和可信 provenance，"
            "生成 frontmatter 并更新索引。不能覆盖已有文件。"
        ),
        risk_level_default="L2", permission_scope="knowledge_vault",
        input_schema={
            "type": "object",
            "properties": {
                "vault_id": {"type": "string", "description": "可选 Vault ID；仅有一个 active Vault 时省略"},
                "title": {
                    "type": "string",
                    "description": (
                        "纯语义文档标题；不得附加运行 ID、Git revision、随机后缀或“精确版”等生成痕迹"
                    ),
                },
                "kind": {"type": "string", "description": "report、note 或 source"},
                "content": {
                    "type": "string",
                    "description": (
                        "Obsidian Markdown 正文；行内公式使用 $...$，块级公式使用 $$...$$"
                    ),
                },
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 20, "description": "最多 20 个标签"},
                "source_urls": {"type": "array", "items": {"type": "string", "format": "uri"}, "maxItems": 50, "description": "最多 50 个公开来源 URL"},
                "provenance_links": {
                    "type": "array",
                    "maxItems": 50,
                    "description": "由 Runtime 从可信工具结果覆盖注入的原文、Artifact 与 RAG 关联；模型不得自行填写",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "source_id": {"type": "string"},
                            "source_url": {"type": "string", "format": "uri"},
                            "artifact_id": {"type": "string"},
                            "artifact_sha256": {"type": "string"},
                            "rag_document_id": {"type": "string"},
                            "rag_job_id": {"type": "string"},
                            "rag_status": {"type": "string"},
                            "rag_search_tool_call_id": {"type": "string"},
                            "rag_chunk_id": {"type": "string"}
                        },
                        "required": ["artifact_id"]
                    }
                },
            },
            "required": ["title", "kind", "content"], "additionalProperties": False,
        },
        allowed_decisions=["allow_once", "deny"],
        metadata={
            "capability": {"id": "knowledge", "version": "1.2.0"},
            "runtime_managed_parameters": ["provenance_links"],
            "agent_prompt": {
                "guidance": (
                    "当用户要求把报告或笔记保存到个人知识库时，必须调用 "
                        "knowledge.create_document；不要改用 workspace.create_file。标题只表达知识主题，"
                        "来源文件名、运行 ID、Git revision 和验证阶段放入来源或 provenance，不进入标题。"
                        "一次总结多份文件时，标题概括共同主题、比较问题或报告周期，不拼接文件名列表。"
                    ),
                "example": {"arguments": {"title": "AI 技术趋势周报（2026年第31周）", "kind": "report", "content": "", "tags": ["周报"], "source_urls": []}, "reason": "用户要求保存报告到 Obsidian"},
            },
        },
    )
    return CapabilityModule(capability_id="knowledge", version="1.2.0", tool_bindings=(ToolBinding(manifest, executor),))
