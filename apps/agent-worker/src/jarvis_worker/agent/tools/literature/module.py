from jarvis_worker.agent.tool_gateway.contracts import ToolManifest
from jarvis_worker.agent.tool_gateway.modules import CapabilityModule, ToolBinding


def create_literature_capability(download_executor, search_executor=None) -> CapabilityModule:
    download_manifest = ToolManifest(
        name="literature.download_arxiv_pdf",
        provider="native",
        description=(
            "按 arXiv ID 下载论文 PDF 到 Jarvis 受控 Artifact Store，校验最终域名、"
            "文件大小、MIME、PDF 文件头和 SHA-256。"
        ),
        risk_level_default="L2",
        permission_scope="artifact_store",
        input_schema={
            "type": "object",
            "properties": {
                "arxiv_id": {
                    "type": "string",
                    "description": "arXiv ID，例如 2401.12345 或 cs/9901001",
                }
            },
            "required": ["arxiv_id"],
            "additionalProperties": False,
        },
        allowed_decisions=["allow_once", "deny"],
        metadata={
            "capability": {"id": "literature", "version": "1.0.0"},
            "agent_prompt": {
                "guidance": (
                    "当用户要求下载可下载的相关来源时，对每个由可信检索结果确认相关且 "
                    "download.available=true 的 arXiv 来源调用；是否可下载以检索结果为准，"
                    "不要根据内容价值自行跳过，也不要把任意 URL 当下载地址。"
                ),
                "example": {
                    "arguments": {"arxiv_id": "2401.12345"},
                    "reason": "用户要求保存这篇 arXiv 论文用于后续研究",
                },
            },
        },
    )
    bindings = [ToolBinding(download_manifest, download_executor)]
    if search_executor is not None:
        search_manifest = ToolManifest(
            name="literature.search_arxiv", provider="native",
            description=(
                "通过第一方受控客户端检索 arXiv；只读、最多返回 10 条，"
                "每条结果包含标准化来源标识、规范 URL、可总结的 abstract 内容、内容指纹，"
                "以及由 provider 确认的 PDF download.available/reference/mime_type，"
                "并对定期报告过滤已收录来源。"
            ),
            risk_level_default="L1", permission_scope="official_arxiv_metadata",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 300},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                },
                "required": ["query"], "additionalProperties": False,
            },
            allowed_decisions=["allow_once", "deny"],
            metadata={"capability": {"id": "literature", "version": "1.1.0"}, "loop": {
                "operation": "retrieve_evidence",
                "evidence_domain": "external_literature.arxiv",
                "substitutable_evidence_domains": [],
            }, "agent_prompt": {
                "guidance": (
                    "检索 arXiv 来源时使用。你负责根据用户目标判断结果是否相关；是否存在可下载原文"
                    "只能读取 result.download.available，不能猜测。若用户要求下载所有可下载的相关"
                    "来源，应逐一调用下载工具，不要再按主观价值筛选。定期任务必须严格使用计划中"
                    "给定的 query 和 max_results。"
                ),
            }},
        )
        bindings.insert(0, ToolBinding(search_manifest, search_executor))
    return CapabilityModule(
        capability_id="literature",
        version="1.1.0",
        tool_bindings=tuple(bindings),
    )
