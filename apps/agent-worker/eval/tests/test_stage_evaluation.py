from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4


EVAL_ROOT = Path(__file__).resolve().parents[1]
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from framework import (  # noqa: E402
    EvaluationStage,
    RagFailureType,
    evaluate_chunking_stage,
    evaluate_embedding_stage,
    evaluate_retrieval_stages,
    evaluate_labeled_trace,
    evaluation_sample_from_context,
    evaluation_sample_from_trace,
    mine_failures,
)
from jarvis_worker.agent.rag.evaluation import (  # noqa: E402
    RagEvaluationLabel,
    RagEvaluationTrace,
)
from jarvis_worker.agent.rag.retrieval import (  # noqa: E402
    RETRIEVAL_POLICY_VERSION,
    RagContextChunk,
    RagContextItem,
    RagContextPackage,
    RagPipelineTrace,
    RagRankedCandidateTrace,
    RagRerankerStepTrace,
    RagRetrievalQuery,
)


def _trace_item(chunk_id, document_id, rank, score):
    return RagRankedCandidateTrace(
        chunk_id=chunk_id,
        document_id=document_id,
        rank=rank,
        score=score,
        content_hash=f"{rank:x}" * 64,
        sources=("semantic",),
        semantic_rank=rank,
    )


def _package(*, drop_in_reranker: bool, drop_in_context: bool):
    workspace_id, document_id = uuid4(), uuid4()
    positive_a, positive_b, negative = uuid4(), uuid4(), uuid4()
    candidates = (
        _trace_item(positive_a, document_id, 1, 0.9),
        _trace_item(negative, document_id, 2, 0.8),
        _trace_item(positive_b, document_id, 3, 0.7),
    )
    reranked = candidates[:1] if drop_in_reranker else (candidates[0], candidates[2])
    context_ids = (positive_a,) if drop_in_context else (positive_a, positive_b)
    chunks = tuple(
        RagContextChunk(
            chunk_id=chunk_id,
            role="primary",
            ordinal=index,
            content=f"Evidence {index}",
            token_count=10,
        )
        for index, chunk_id in enumerate(context_ids)
    )
    item = RagContextItem(
        chunk_id=positive_a,
        document_id=document_id,
        document_title="Document",
        source_artifact_id=uuid4(),
        score=0.9,
        chunks=chunks,
        elements=(),
        token_count=20,
    )
    return (
        RagContextPackage(
            query="What is the evidence?",
            workspace_id=workspace_id,
            policy_version=RETRIEVAL_POLICY_VERSION,
            items=(item,),
            candidate_count=len(candidates),
            total_tokens=20,
            token_budget=100,
            truncated=False,
            pipeline=RagPipelineTrace(
                query_rewriter="identity",
                retriever="hybrid",
                reranker="policy",
                context_assembler="evidence",
                queries=("What is the evidence?",),
                retrieved_candidates=candidates,
                reranked_candidates=reranked,
                context_chunk_ids=context_ids,
                reranker_steps=(
                    RagRerankerStepTrace(
                        stage_id="semantic-rank-blend-v1",
                        status="degraded",
                        provider="test-provider",
                        model="test-model",
                        input_count=len(candidates),
                        output_count=len(candidates),
                        latency_ms=12,
                        failure_code="RERANKER_TIMEOUT",
                    ),
                ),
            ),
        ),
        {str(positive_a), str(positive_b)},
        {str(negative)},
    )


def test_real_context_projection_and_metrics_find_reranker_drop():
    package, positives, negatives = _package(drop_in_reranker=True, drop_in_context=False)
    sample = evaluation_sample_from_context(
        package,
        trace_id="trace-1",
        query_id="query-1",
        positive_chunk_ids=positives,
        hard_negative_chunk_ids=negatives,
    )

    metrics = evaluate_retrieval_stages(sample, cutoffs=(1, 3, 5))
    by_id = {metric.metric_id: metric.value for metric in metrics}
    failures = mine_failures(sample, metrics)

    assert by_id["candidate.recall@5"] == 1.0
    assert by_id["reranker.recall@5"] == 0.5
    assert by_id["reranker.evidence_drop_rate"] == 0.5
    assert [failure.failure_type for failure in failures] == [
        RagFailureType.RERANKER_EVIDENCE_DROPPED
    ]
    assert failures[0].privacy_status == "pending"


def test_metrics_distinguish_context_drop_from_reranker_drop():
    package, positives, negatives = _package(drop_in_reranker=False, drop_in_context=True)
    sample = evaluation_sample_from_context(
        package,
        trace_id="trace-2",
        query_id="query-2",
        positive_chunk_ids=positives,
        hard_negative_chunk_ids=negatives,
    )

    metrics = evaluate_retrieval_stages(sample, cutoffs=(5,))
    failures = mine_failures(sample, metrics)

    assert [failure.failure_type for failure in failures] == [
        RagFailureType.CONTEXT_EVIDENCE_DROPPED
    ]
    assert failures[0].suspected_stage is EvaluationStage.CONTEXT_ASSEMBLY


def test_embedding_and_chunk_metrics_do_not_persist_vectors():
    embedding = evaluate_embedding_stage(
        query_vector=(1.0, 0.0),
        positive_vectors={"positive": (0.9, 0.1)},
        hard_negative_vectors={"negative": (0.0, 1.0)},
    )
    chunking = evaluate_chunking_stage(
        chunk_node_ids=(("a", "b"), ("c",)),
        token_counts=(120, 20),
        must_keep_groups=(("a", "b"), ("b", "c")),
        min_tokens=80,
        max_tokens=700,
    )
    values = {metric.metric_id: metric.value for metric in (*embedding, *chunking)}

    assert values["embedding.positive_negative_margin"] > 0.9
    assert values["embedding.pairwise_ranking_accuracy"] == 1.0
    assert values["chunk.must_keep_pass_rate"] == 0.5
    assert values["chunk.short_chunk_rate"] == 0.5
    assert all("vector" not in metric.details for metric in embedding)


def test_confirmed_production_trace_runs_through_failure_orchestrator():
    package, positives, negatives = _package(drop_in_reranker=True, drop_in_context=False)
    trace = replace(
        RagEvaluationTrace.capture(
            task_id=uuid4(),
            run_id=uuid4(),
            step_id=uuid4(),
            request=RagRetrievalQuery(query=package.query),
            package=package,
        ),
        privacy_status="approved",
    )
    label = RagEvaluationLabel(
        id=uuid4(),
        trace_id=trace.id,
        positive_chunk_ids=tuple(UUID(value) for value in positives),
        hard_negative_chunk_ids=tuple(UUID(value) for value in negatives),
        status="confirmed",
    )

    sample = evaluation_sample_from_trace(trace, label)
    outcome = evaluate_labeled_trace(sample, cutoffs=(5,))

    assert trace.query_hash
    assert trace.pipeline_versions["reranker_execution"] == (
        "semantic-rank-blend-v1:degraded:test-provider:test-model:RERANKER_TIMEOUT"
    )
    assert trace.pipeline_versions["reranker_latency_ms"] == "12"
    assert all("content" not in value for value in trace.candidate_ranking)
    assert [failure.failure_type for failure in outcome.failures] == [
        RagFailureType.RERANKER_EVIDENCE_DROPPED
    ]
