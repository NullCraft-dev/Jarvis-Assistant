from __future__ import annotations

import hashlib
import sys
from pathlib import Path

RUNNERS = Path(__file__).resolve().parents[1] / "runners"
if str(RUNNERS) not in sys.path:
    sys.path.insert(0, str(RUNNERS))

from pipeline_eval import (  # noqa: E402
    align_gold_nodes,
    evaluate_chunking,
    evaluate_preprocessing,
    evaluate_retrieval,
    evaluate_routing,
    rank_chunks,
)

from jarvis_worker.agent.rag.chunking.contracts import (  # noqa: E402
    ChunkDraft,
    ChunkModality,
)
from jarvis_worker.agent.rag.preprocessing.contracts import (  # noqa: E402
    DocumentNode,
    DocumentNodeType,
    NodeExtractionMethod,
)


def _node(label: str, node_type: DocumentNodeType, text: str, order: int) -> DocumentNode:
    return DocumentNode(
        node_id=hashlib.sha256(label.encode()).hexdigest(),
        node_type=node_type,
        page_number=1,
        order_index=order,
        bounding_box=(10.0, 10.0 + order * 30, 90.0, 30.0 + order * 30),
        page_width=100.0,
        page_height=100.0,
        extraction_method=NodeExtractionMethod.HYBRID,
        extraction_version="test",
        confidence=0.9,
        text=text,
    )


def _chunk(ordinal: int, content: str, node_ids: tuple[str, ...]) -> ChunkDraft:
    return ChunkDraft(
        ordinal=ordinal,
        content=content,
        token_count=10,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        page_start=1,
        page_end=1,
        block_start=ordinal,
        block_end=ordinal,
        heading_path=(),
        modality=ChunkModality.TEXT,
        node_ids=node_ids,
    )


def _annotation() -> dict:
    return {
        "pages": [
            {
                "page_number": 1,
                "expected_route": "structure-model",
                "nodes": [
                    {
                        "gold_id": "g1",
                        "type": "heading",
                        "indexable": True,
                        "text": "Heading",
                        "bounding_box": [10, 10, 90, 30],
                    },
                    {
                        "gold_id": "g2",
                        "type": "paragraph",
                        "indexable": True,
                        "text": "Evidence text",
                        "bounding_box": [10, 40, 90, 60],
                    },
                ],
            },
            {
                "page_number": 2,
                "expected_route": "native",
                "nodes": [],
            },
        ],
        "chunking": {"must_keep_together": [["g1", "g2"]]},
        "queries": [
            {
                "query_id": "q1",
                "query": "evidence?",
                "answerable": True,
                "evidence_gold_ids": ["g2"],
            },
            {
                "query_id": "q2",
                "query": "unknown?",
                "answerable": False,
                "evidence_gold_ids": [],
            },
        ],
    }


def test_routing_reports_false_positive_without_hiding_recall() -> None:
    metrics = evaluate_routing(_annotation(), [1, 2])

    assert metrics["recall"] == 1.0
    assert metrics["precision"] == 0.5
    assert metrics["false_positive_pages"] == [2]


def test_alignment_drives_preprocessing_and_chunk_metrics() -> None:
    nodes = (
        _node("heading", DocumentNodeType.HEADING, "Heading", 0),
        _node("body", DocumentNodeType.PARAGRAPH, "Evidence text", 1),
    )
    annotation = _annotation()
    matches, details = align_gold_nodes(annotation, nodes)
    preprocessing = evaluate_preprocessing(annotation, nodes, matches, details)
    node_ids = tuple(node_id for output_ids in matches.values() for node_id in output_ids)
    chunks = (_chunk(0, "Heading\n\nEvidence text", node_ids),)
    chunking = evaluate_chunking(annotation, chunks, matches)

    assert set(matches) == {"g1", "g2"}
    assert preprocessing["indexable_gold_node_recall"] == 1.0
    assert preprocessing["matched_type_accuracy"] == 1.0
    assert chunking["must_keep_group_pass_rate"] == 1.0


def test_retrieval_metrics_use_gold_to_output_alignment() -> None:
    body = _node("body", DocumentNodeType.PARAGRAPH, "Evidence text", 1)
    chunks = (
        _chunk(0, "irrelevant", ()),
        _chunk(1, "Evidence text", (body.node_id,)),
    )
    rankings = {"q1": [(1, 0.9), (0, 0.1)], "q2": [(0, 0.8), (1, 0.2)]}

    metrics = evaluate_retrieval(_annotation(), chunks, rankings, {"g2": [body.node_id]})

    assert metrics["mean_recall_at_k"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["production_retrieval_service_exercised"] is False


def test_cosine_ranking_is_deterministic() -> None:
    ranking = rank_chunks([1.0, 0.0], [[0.0, 1.0], [1.0, 0.0]], limit=2)

    assert ranking == [(1, 1.0), (0, 0.0)]
