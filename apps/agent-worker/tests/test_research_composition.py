from __future__ import annotations

from uuid import uuid4

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.runner import AgentRunner
from jarvis_worker.agent.intents import (
    IntentEffects,
    IntentExtraction,
    RetrievalIntent,
)
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest, ToolResult
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.runtime_bus.messages import RunJobMessage


class _DecisionModel(ModelProvider):
    def __init__(self, actions: list[AgentAction]) -> None:
        self._actions = iter(actions)

    def decide_next_action(self, _state):
        return next(self._actions)


class _KnowledgeWriteIntent:
    uses_model = False

    def __init__(self, *, provenance: str, title: str = "") -> None:
        self._provenance = provenance
        self._title = title

    def extract(self, *_args, **_kwargs):
        return IntentExtraction(
            primary_intent="knowledge_write",
            retrieval=RetrievalIntent(
                mode="skip",
                query="",
                confidence=1.0,
                reason="用户要求写入知识库",
                document_scope="none",
            ),
            effects=IntentEffects(
                knowledge_write="required",
                knowledge_provenance=self._provenance,
                knowledge_title=self._title,
            ),
            source="rule",
        )


def test_llm_composes_knowledge_and_rag_without_product_skill():
    artifact_id = str(uuid4())
    rag_document_id = str(uuid4())
    rag_job_id = str(uuid4())
    captured_knowledge_arguments: list[dict] = []
    calls: list[str] = []
    registry = ToolRegistry()

    def register(name, executor):
        def observed(request):
            calls.append(request.tool_name)
            return executor(request)

        registry.register(
            ToolManifest(name=name, risk_level_default="L1"), observed
        )

    register(
        "literature.search_arxiv",
        lambda _request: ToolResult(
            ok=True,
            kind="json",
            data={
                "source": "arxiv",
                "results": [
                    {
                        "arxiv_id": "2607.24368v1",
                        "source_id": "arxiv:2607.24368v1",
                        "canonical_url": "https://arxiv.org/abs/2607.24368v1",
                        "content_scope": "abstract",
                        "content_text": "Retrieved abstract evidence.",
                        "download": {
                            "available": True,
                            "reference": "2607.24368v1",
                            "mime_type": "application/pdf",
                        },
                    }
                ],
            },
        ),
    )
    register(
        "literature.download_arxiv_pdf",
        lambda _request: ToolResult(
            ok=True,
            kind="file",
            data={"arxiv_id": "2607.24368v1", "sha256": "a" * 64},
            artifact_ids=[artifact_id],
        ),
    )
    register(
        "rag.ingest_artifact",
        lambda _request: ToolResult(
            ok=True,
            kind="json",
            data={
                "artifact_id": artifact_id,
                "document_id": rag_document_id,
                "job_id": rag_job_id,
                "status": "queued",
            },
        ),
    )

    def create_knowledge(request):
        captured_knowledge_arguments.append(dict(request.arguments))
        return ToolResult(ok=True, kind="json", data={"document_id": str(uuid4())})

    register("knowledge.create_document", create_knowledge)

    model = _DecisionModel(
        [
            AgentAction.call_tool(
                "literature.search_arxiv", {"query": "agent memory"}
            ),
            AgentAction.call_tool(
                "literature.download_arxiv_pdf",
                {"arxiv_id": "2607.24368v1"},
            ),
            AgentAction.call_tool(
                "rag.ingest_artifact", {"artifact_id": artifact_id}
            ),
            AgentAction.call_tool(
                "knowledge.create_document",
                {
                    "title": "Agent Memory",
                    "kind": "report",
                    "content": "Evidence-based summary.",
                    "source_urls": ["https://arxiv.org/abs/2607.24368v1"],
                },
            ),
            AgentAction.finish("知识报告已保存，RAG 作业已进入队列。"),
        ]
    )
    runner = AgentRunner(
        model,
        ToolGateway(registry, PermissionManager()),
        max_iterations=6,
    )
    job = RunJobMessage(
        job_id=str(uuid4()),
        trace_id=str(uuid4()),
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        user_goal="研究论文，下载所有可下载的相关原文，保存知识报告并加入 RAG。",
        created_at="2026-07-30T00:00:00Z",
    )

    events = runner.run(job)

    assert calls == [
        "literature.search_arxiv",
        "literature.download_arxiv_pdf",
        "rag.ingest_artifact",
        "knowledge.create_document",
    ]
    assert not any(name.startswith("skill.") for name in calls)
    assert events[-1].event_type == "agent.run.completed"
    started_step_ids = [
        event.runtime_event["step_id"]
        for event in events
        if event.event_type in {"model.call.started", "tool.call.started"}
    ]
    assert len(started_step_ids) == len(set(started_step_ids))
    assert captured_knowledge_arguments[0]["provenance_links"] == [
        {
            "source_id": "arxiv:2607.24368v1",
            "source_url": "https://arxiv.org/abs/2607.24368v1",
            "artifact_id": artifact_id,
            "artifact_sha256": "a" * 64,
            "rag_document_id": rag_document_id,
            "rag_job_id": rag_job_id,
            "rag_status": "queued",
        }
    ]


def test_runtime_overrides_model_provenance_with_rag_search_evidence():
    artifact_id = str(uuid4())
    rag_document_id = str(uuid4())
    primary_chunk_id = str(uuid4())
    neighbour_chunk_id = str(uuid4())
    captured_knowledge_arguments: list[dict] = []
    registry = ToolRegistry()

    registry.register(
        ToolManifest(name="rag.search", risk_level_default="L1"),
        lambda _request: ToolResult(
            ok=True,
            kind="json",
            data={
                "results": [{
                    "document_id": rag_document_id,
                    "document_title": "Trusted document",
                    "source_artifact_id": artifact_id,
                    "chunks": [
                        {"chunk_id": primary_chunk_id, "role": "primary"},
                        {"chunk_id": neighbour_chunk_id, "role": "next"},
                    ],
                }]
            },
        ),
    )

    def create_knowledge(request):
        captured_knowledge_arguments.append(dict(request.arguments))
        return ToolResult(ok=True, kind="json", data={"document_id": str(uuid4())})

    registry.register(
        ToolManifest(name="knowledge.create_document", risk_level_default="L1"),
        create_knowledge,
    )
    runner = AgentRunner(
        _DecisionModel([
            AgentAction.call_tool("rag.search", {"query": "trusted evidence"}),
            AgentAction.call_tool(
                "knowledge.create_document",
                {
                    "title": "RAG note",
                    "kind": "note",
                    "content": "Summary from retrieved evidence.",
                    "provenance_links": [{
                        "artifact_id": str(uuid4()),
                        "rag_document_id": str(uuid4()),
                    }],
                },
            ),
            AgentAction.finish("知识笔记已保存。"),
        ]),
        ToolGateway(registry, PermissionManager()),
        max_iterations=4,
    )
    job = RunJobMessage(
        job_id=str(uuid4()), trace_id=str(uuid4()), task_id=str(uuid4()),
        run_id=str(uuid4()), user_goal="检索个人资料并保存一篇知识笔记。",
        created_at="2026-07-30T00:00:00Z",
    )

    events = runner.run(job)

    rag_tool_call_id = next(
        event.runtime_event["payload"]["tool_call"]["id"]
        for event in events
        if event.event_type == "tool.call.started"
        and event.runtime_event["payload"]["tool_call"]["tool_name"] == "rag.search"
    )
    assert captured_knowledge_arguments[0]["provenance_links"] == [
        {
            "artifact_id": artifact_id,
            "rag_document_id": rag_document_id,
            "rag_search_tool_call_id": rag_tool_call_id,
            "rag_chunk_id": primary_chunk_id,
        },
        {
            "artifact_id": artifact_id,
            "rag_document_id": rag_document_id,
            "rag_search_tool_call_id": rag_tool_call_id,
            "rag_chunk_id": neighbour_chunk_id,
        },
    ]


def test_follow_up_knowledge_write_uses_runtime_history_provenance_and_exact_title():
    artifact_id = str(uuid4())
    document_id = str(uuid4())
    search_call_id = str(uuid4())
    chunk_id = str(uuid4())
    historical = [{
        "artifact_id": artifact_id,
        "rag_document_id": document_id,
        "rag_search_tool_call_id": search_call_id,
        "rag_chunk_id": chunk_id,
    }]
    captured: list[dict] = []
    registry = ToolRegistry()
    registry.register(
        ToolManifest(name="knowledge.create_document", risk_level_default="L1"),
        lambda request: captured.append(dict(request.arguments))
        or ToolResult(ok=True, kind="json", data={"document_id": str(uuid4())}),
    )
    runner = AgentRunner(
        _DecisionModel([
            AgentAction.call_tool(
                "knowledge.create_document",
                {
                    "title": "模型擅自扩写的标题",
                    "kind": "note",
                    "content": "上一轮比较的摘要。",
                },
            ),
            AgentAction.finish("知识笔记已保存。"),
        ]),
        ToolGateway(registry, PermissionManager()),
        intent_extractor=_KnowledgeWriteIntent(
            provenance="required", title="风险管理框架比较"
        ),
    )
    job = RunJobMessage(
        job_id=str(uuid4()), trace_id=str(uuid4()), task_id=str(uuid4()),
        run_id=str(uuid4()),
        user_goal="把刚才的比较写成知识库笔记，标题叫风险管理框架比较，保留来源。",
        created_at="2026-08-10T00:00:00Z",
    )

    events = runner.run(job, trusted_history_provenance=historical)

    assert events[-1].event_type == "agent.run.completed"
    assert captured[0]["title"] == "风险管理框架比较"
    assert captured[0]["provenance_links"] == historical


def test_required_knowledge_provenance_fails_before_tool_execution():
    executed: list[bool] = []
    registry = ToolRegistry()
    registry.register(
        ToolManifest(name="knowledge.create_document", risk_level_default="L2"),
        lambda _request: executed.append(True) or ToolResult(ok=True),
    )
    runner = AgentRunner(
        _DecisionModel([
            AgentAction.call_tool(
                "knowledge.create_document",
                {"title": "笔记", "kind": "note", "content": "正文"},
            )
        ]),
        ToolGateway(registry, PermissionManager()),
        intent_extractor=_KnowledgeWriteIntent(provenance="required"),
    )
    job = RunJobMessage(
        job_id=str(uuid4()), trace_id=str(uuid4()), task_id=str(uuid4()),
        run_id=str(uuid4()), user_goal="保存笔记并保留来源",
        created_at="2026-08-10T00:00:00Z",
    )

    events = runner.run(job)

    assert executed == []
    assert not any(event.event_type == "permission.required" for event in events)
    assert events[-1].event_type == "agent.run.failed"
    assert events[-1].runtime_event["payload"]["error"]["code"] == (
        "KNOWLEDGE_PROVENANCE_REQUIRED"
    )
