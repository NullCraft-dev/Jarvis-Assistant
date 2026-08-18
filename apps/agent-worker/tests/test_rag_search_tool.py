from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.rag.retrieval import (
    RETRIEVAL_POLICY_VERSION,
    RagContextChunk,
    RagContextItem,
    RagContextPackage,
    RagPipelineTrace,
)
from jarvis_worker.agent.tool_gateway.contracts import ToolRequest
from jarvis_worker.agent.tools.rag import RagSearchToolExecutor, create_rag_capability


class _Bridge:
    def run(self, coroutine, *, timeout):
        assert timeout in (10, 30)
        return asyncio.run(coroutine)


class _Service:
    def __init__(self, package):
        self.package = package
        self.calls = []

    async def search_for_task(self, **kwargs):
        self.calls.append(kwargs)
        return self.package


class _TraceService:
    def __init__(self):
        self.calls = []
        self.trace_id = uuid4()

    async def capture(self, **kwargs):
        self.calls.append(kwargs)
        return type("Trace", (), {"id": self.trace_id})()


def _package():
    workspace_id, document_id, chunk_id = uuid4(), uuid4(), uuid4()
    chunk = RagContextChunk(
        chunk_id=chunk_id,
        role="primary",
        ordinal=0,
        content="Retrieved evidence",
        token_count=12,
        source_locator={"page_start": 1},
    )
    item = RagContextItem(
        chunk_id=chunk_id,
        document_id=document_id,
        document_title="Technical paper",
        source_artifact_id=uuid4(),
        score=0.91,
        chunks=(chunk,),
        elements=(),
        token_count=12,
    )
    return RagContextPackage(
        query="technical question",
        workspace_id=workspace_id,
        policy_version=RETRIEVAL_POLICY_VERSION,
        items=(item,),
        candidate_count=4,
        total_tokens=12,
        token_budget=4_000,
        truncated=False,
        pipeline=RagPipelineTrace(
            query_rewriter="identity",
            retriever="hybrid",
            reranker="policy",
            context_assembler="evidence",
            queries=("technical question",),
        ),
    )


def test_rag_search_tool_uses_task_scope_and_returns_citations():
    package = _package()
    service = _Service(package)
    executor = RagSearchToolExecutor(service, _Bridge())
    task_id = uuid4()

    result = executor(
        ToolRequest(
            task_id=str(task_id),
            run_id=str(uuid4()),
            tool_name="rag.search",
            arguments={"query": "technical question", "top_k": 6},
        )
    )

    assert result.ok is True
    assert result.data["workspace_id"] == str(package.workspace_id)
    assert result.data["results"][0]["chunks"][0]["source_locator"] == {"page_start": 1}
    assert service.calls[0]["task_id"] == task_id
    assert not hasattr(service.calls[0]["request"], "workspace_id")
    assert "retrieved_candidates" not in result.data["pipeline"]
    assert "reranked_candidates" not in result.data["pipeline"]
    assert result.data["evidence_assessment"] == {
        "schema": "rag-evidence-assessment-v1",
        "policy_version": "rag-evidence-sufficiency-v2",
        "sufficient": True,
        "reason_code": "SUFFICIENT",
        "evidence_count": 1,
        "covered_document_count": 1,
        "requested_document_count": 0,
        "strict_anchor_count": 0,
        "covered_strict_anchor_count": 0,
        "lexical_gate_applied": True,
        "lexical_term_count": 2,
        "covered_lexical_term_count": 1,
    }


def test_rag_search_fails_sufficiency_when_exact_query_constraint_is_uncovered():
    package = replace(
        _package(),
        query="2035 quantum computing market forecast",
    )
    result = RagSearchToolExecutor(_Service(package), _Bridge())(
        ToolRequest(
            task_id=str(uuid4()),
            run_id=str(uuid4()),
            tool_name="rag.search",
            arguments={"query": package.query},
        )
    )

    assessment = result.data["evidence_assessment"]
    assert assessment["sufficient"] is False
    assert assessment["reason_code"] == "QUERY_CONSTRAINT_UNCOVERED"
    assert assessment["strict_anchor_count"] == 1
    assert assessment["covered_strict_anchor_count"] == 0


def test_rag_search_fails_sufficiency_when_same_script_has_no_lexical_anchor():
    package = _package()
    package = replace(
        package,
        query="quantum computing market forecast",
        pipeline=replace(
            package.pipeline,
            queries=("quantum computing market forecast",),
        ),
    )
    result = RagSearchToolExecutor(_Service(package), _Bridge())(
        ToolRequest(
            task_id=str(uuid4()),
            run_id=str(uuid4()),
            tool_name="rag.search",
            arguments={"query": package.query},
        )
    )

    assessment = result.data["evidence_assessment"]
    assert assessment["sufficient"] is False
    assert assessment["reason_code"] == "QUERY_EVIDENCE_LEXICAL_MISMATCH"
    assert assessment["lexical_gate_applied"] is True
    assert assessment["covered_lexical_term_count"] == 0


def test_rag_search_tool_captures_production_trace_without_exposing_rankings():
    package = _package()
    service = _Service(package)
    trace_service = _TraceService()
    executor = RagSearchToolExecutor(service, _Bridge(), trace_service=trace_service)
    task_id, run_id, step_id = uuid4(), uuid4(), uuid4()

    result = executor(
        ToolRequest(
            task_id=str(task_id),
            run_id=str(run_id),
            step_id=str(step_id),
            tool_name="rag.search",
            arguments={"query": "technical question"},
        )
    )

    assert result.ok is True
    assert result.data["evaluation_trace_id"] == str(trace_service.trace_id)
    assert trace_service.calls[0]["task_id"] == task_id
    assert trace_service.calls[0]["run_id"] == run_id
    assert trace_service.calls[0]["step_id"] == step_id
    assert "retrieved_candidates" not in result.data["pipeline"]


def test_rag_search_tool_rejects_model_supplied_workspace_argument():
    executor = RagSearchToolExecutor(_Service(_package()), _Bridge())
    result = executor(
        ToolRequest(
            task_id=str(uuid4()),
            run_id=str(uuid4()),
            tool_name="rag.search",
            arguments={"query": "question", "document_ids": ["not-a-uuid"]},
        )
    )
    assert result.ok is False
    assert result.error["code"] == "RAG_SEARCH_ARGUMENTS_INVALID"


def test_rag_search_tool_expands_top_k_and_reports_selected_document_coverage():
    package = _package()
    service = _Service(package)
    executor = RagSearchToolExecutor(service, _Bridge())
    covered_document_id = package.items[0].document_id
    missing_document_id = uuid4()

    result = executor(
        ToolRequest(
            task_id=str(uuid4()),
            run_id=str(uuid4()),
            tool_name="rag.search",
            arguments={
                "query": "比较两份资料",
                "top_k": 1,
                "document_ids": [
                    str(covered_document_id),
                    str(missing_document_id),
                ],
            },
        )
    )

    assert result.ok is True
    assert service.calls[0]["request"].top_k == 2
    assert result.data["document_coverage"] == {
        "requested_count": 2,
        "covered_count": 1,
        "complete": False,
        "uncovered_document_ids": [str(missing_document_id)],
    }
    assert result.metadata["document_coverage_complete"] is False
    assert result.metadata["evidence_sufficient"] is False
    assert (
        result.data["evidence_assessment"]["reason_code"]
        == "REQUESTED_DOCUMENT_COVERAGE_INCOMPLETE"
    )
    assert "指定文档覆盖 1/2" in result.summary
    assert "证据不足" in result.summary


def test_rag_search_manifest_is_allowlisted_l0():
    executor = RagSearchToolExecutor(_Service(_package()), _Bridge())
    manifest = create_rag_capability(executor).tool_bindings[0].manifest
    request = ToolRequest(
        task_id=str(uuid4()),
        run_id=str(uuid4()),
        tool_name="rag.search",
        arguments={"query": "question"},
    )

    decision = PermissionManager().check(manifest, request)

    assert manifest.risk_level_default == "L0"
    assert "workspace_id" not in manifest.input_schema["properties"]
    assert manifest.input_schema["properties"]["top_k"]["maximum"] == 20
    assert decision.allowed is True
    assert decision.needs_user_approval is False
