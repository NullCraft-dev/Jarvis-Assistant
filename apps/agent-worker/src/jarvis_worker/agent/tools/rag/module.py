"""RAG read-only capability module。"""

from jarvis_worker.agent.tool_gateway.contracts import ToolManifest
from jarvis_worker.agent.tool_gateway.modules import CapabilityModule, ToolBinding


def create_rag_capability(
    search_executor,
    ingestion_executor=None,
    await_ingestion_executor=None,
) -> CapabilityModule:
    search_manifest = ToolManifest(
        name="rag.search",
        provider="native",
        description=(
            "在当前任务绑定的 Workspace 内检索已完成索引的专业文档，返回带页码、章节、"
            "相邻文本以及相关表格/图片元素 ID 的引用证据。只读且不能跨 Workspace。"
        ),
        risk_level_default="L0",
        permission_scope="current_workspace_rag",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2_000,
                    "description": "需要从专业文档中检索的问题或概念",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                },
                "document_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": "可选；把检索限制在当前 Workspace 的指定 RAG 文档",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        allowed_decisions=[],
        metadata={
            "capability": {"id": "rag", "version": "1.0.0"},
            "loop": {
                "operation": "retrieve_evidence",
                "evidence_domain": "workspace.indexed_documents",
                "substitutable_evidence_domains": [],
            },
            "agent_prompt": {
                "guidance": (
                    "当用户询问已入库文档中的专业事实、概念、表格或图表时调用 rag.search；"
                    "Runtime 指定多份文档时会保证 top_k 不小于文档数，并在 document_coverage 中返回"
                    "覆盖完整性；覆盖不完整时不得声称已完成全面比较或总结。"
                    "调用成功后，基于证据回答必须在 finish.citations 中至少提交一个工具返回的 "
                    'chunk_id，格式为 [{"chunk_id":"UUID"}]，不得提交文档名、页码等可信元数据；'
                    "证据不足时设置 insufficient_evidence=true 且 citations=[]。"
                ),
                "example": {
                    "arguments": {
                        "query": "PaddleOCR-VL 如何处理复杂页面中的图表？",
                        "top_k": 6,
                    },
                    "reason": "用户的问题需要从已入库的专业资料中查证",
                },
            },
        },
    )
    bindings = [ToolBinding(search_manifest, search_executor)]
    if ingestion_executor is not None:
        ingest_manifest = ToolManifest(
            name="rag.ingest_artifact",
            provider="native",
            description=(
                "将当前任务 Workspace 内、由已完成 ToolGateway 调用产生的受控 PDF Artifact "
                "加入 RAG 预处理、分块和向量化队列。不会读取 Obsidian Markdown。"
            ),
            risk_level_default="L2",
            permission_scope="current_workspace_rag_ingestion",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "已持久化的可信 PDF Artifact ID",
                    }
                },
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
            allowed_decisions=["allow_once", "deny"],
            metadata={
                "capability": {"id": "rag", "version": "1.1.0"},
                "agent_prompt": {
                    "guidance": (
                        "只有用户明确要求把已下载或已上传 PDF 加入 RAG 时才调用；若用户要求把所有"
                        "已下载的相关资料加入 RAG，应对每个成功下载返回的 artifact_id 分别调用，"
                        "不要再按内容价值二次筛选。"
                        "artifact_id 必须来自可信工具结果，不能自行编造。入队成功只表示异步摄取已开始，"
                        "不能宣称文档已经完成向量化；最终回复必须逐字使用 ToolResult.data 中的 "
                        "document_id、job_id 和 status，不得声称这些字段缺失。"
                    ),
                    "example": {
                        "arguments": {"artifact_id": "00000000-0000-4000-8000-000000000000"},
                        "reason": "用户明确要求把刚下载的 PDF 加入 RAG",
                    },
                },
            },
        )
        bindings.append(ToolBinding(ingest_manifest, ingestion_executor))
    if await_ingestion_executor is not None:
        await_manifest = ToolManifest(
            name="rag.await_ingestion",
            provider="native",
            description=(
                "等待当前任务已提交的 RAG 入库作业完成解析、分块和向量化，并返回真实终态。"
                "只读，不创建或修改作业。"
            ),
            risk_level_default="L0",
            permission_scope="current_workspace_rag",
            input_schema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "rag.ingest_artifact 返回的可信作业 ID",
                    }
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
            allowed_decisions=[],
            metadata={
                "capability": {"id": "rag", "version": "1.2.0"},
                "agent_prompt": {
                    "guidance": (
                        "rag.ingest_artifact 返回 queued 只表示已入队。若用户明确要求在资料真正可检索、"
                        "向量化完成或成功后再告知，必须使用其 ToolResult.data.job_id 调用本工具，"
                        "并且只有 ready=true 后才能声称完成。工具名必须逐字使用 rag.await_ingestion，"
                        "不得自造同义工具名。若用户只要求提交后台处理，则无需等待。"
                    ),
                    "example": {
                        "arguments": {"job_id": "00000000-0000-4000-8000-000000000000"},
                        "reason": "用户要求在论文真正可检索后再告知",
                    },
                    "always_include_example": True,
                },
            },
        )
        bindings.append(ToolBinding(await_manifest, await_ingestion_executor))
    return CapabilityModule(
        capability_id="rag",
        version=(
            "1.2.0"
            if await_ingestion_executor is not None
            else ("1.1.0" if ingestion_executor is not None else "1.0.0")
        ),
        tool_bindings=tuple(bindings),
    )
