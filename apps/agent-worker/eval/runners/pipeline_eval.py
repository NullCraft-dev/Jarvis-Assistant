"""RAG 完整链路评估的纯计算与产物序列化辅助。

本模块不复制生产解析、预处理或分片逻辑。调用方必须注入生产对象；这里仅负责
记录中间产物、将输出与金标对齐，以及计算路由、结构、分片和检索指标。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any

from jarvis_worker.agent.rag.chunking.contracts import ChunkDraft
from jarvis_worker.agent.rag.preprocessing.contracts import (
    DocumentNode,
    StructurePageResult,
)

_SPACE = re.compile(r"\s+")
EVALUATOR_VERSION = "pipeline-eval-v2"


def serialize_node(node: DocumentNode, *, include_asset_metadata: bool = True) -> dict[str, Any]:
    """Serialize a node without ever writing binary PDF assets to the report."""

    value = {
        "node_id": node.node_id,
        "type": node.node_type.value,
        "page_number": node.page_number,
        "order_index": node.order_index,
        "bounding_box": list(node.bounding_box),
        "page_width": node.page_width,
        "page_height": node.page_height,
        "extraction_method": node.extraction_method.value,
        "extraction_version": node.extraction_version,
        "confidence": node.confidence,
        "text": node.text,
        "structured_data": node.structured_data,
        "parent_node_id": node.parent_node_id,
        "related_node_ids": list(node.related_node_ids),
    }
    if include_asset_metadata:
        value["asset"] = {
            "present": node.asset_bytes is not None,
            "mime_type": node.asset_mime_type,
            "size_bytes": len(node.asset_bytes) if node.asset_bytes is not None else 0,
        }
    return value


def serialize_structure_result(result: StructurePageResult) -> dict[str, Any]:
    return {
        "page_number": result.page_number,
        "provider": result.provider,
        "provider_version": result.provider_version,
        "nodes": [serialize_node(node) for node in result.nodes],
    }


def serialize_chunk(chunk: ChunkDraft) -> dict[str, Any]:
    value = asdict(chunk)
    value["modality"] = chunk.modality.value
    value["heading_path"] = list(chunk.heading_path)
    value["node_ids"] = list(chunk.node_ids)
    value["element_node_ids"] = list(chunk.element_node_ids)
    return value


class RecordingStructureProvider:
    """Transparent wrapper used to retain raw PaddleOCR-VL page results."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self.provider_name = provider.provider_name
        self.provider_version = provider.provider_version
        self.results: list[StructurePageResult] = []

    async def analyze_page(self, **kwargs: Any) -> StructurePageResult:
        result = await self._provider.analyze_page(**kwargs)
        self.results.append(result)
        return result


def evaluate_routing(annotation: dict[str, Any], selected_pages: Sequence[int]) -> dict[str, Any]:
    expected = {
        int(page["page_number"])
        for page in annotation["pages"]
        if page["expected_route"] == "structure-model"
    }
    selected = set(selected_pages)
    true_positive = expected & selected
    false_positive = selected - expected
    false_negative = expected - selected
    precision = len(true_positive) / len(selected) if selected else (1.0 if not expected else 0.0)
    recall = len(true_positive) / len(expected) if expected else 1.0
    return {
        "expected_structure_pages": sorted(expected),
        "selected_structure_pages": sorted(selected),
        "true_positive_pages": sorted(true_positive),
        "false_positive_pages": sorted(false_positive),
        "false_negative_pages": sorted(false_negative),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
    }


def align_gold_nodes(
    annotation: dict[str, Any], nodes: Sequence[DocumentNode]
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Align one semantic gold region to one or more production output nodes."""

    output_by_page: dict[int, list[DocumentNode]] = {}
    for node in nodes:
        output_by_page.setdefault(node.page_number, []).append(node)

    matches: dict[str, list[str]] = {}
    details: list[dict[str, Any]] = []
    used: set[str] = set()
    for page in annotation["pages"]:
        page_number = int(page["page_number"])
        for gold in page["nodes"]:
            candidates: list[tuple[float, DocumentNode, dict[str, Any]]] = []
            for node in output_by_page.get(page_number, []):
                if node.node_id in used:
                    continue
                parts = _node_match_parts(gold, node)
                score = parts["score"]
                if score >= 0.45:
                    candidates.append((score, node, parts))
            candidates.sort(key=lambda item: item[0], reverse=True)
            if not candidates:
                details.append(
                    {
                        "gold_id": gold["gold_id"],
                        "output_node_ids": [],
                        "score": 0.0,
                        "type_match": False,
                        "semantic_family_match": False,
                        "text_similarity": 0.0,
                        "geometry_overlap": 0.0,
                    }
                )
                continue
            selected = [
                candidate
                for candidate in candidates
                if candidate[2]["geometry_overlap"] >= 0.7
                and candidate[2]["semantic_family_match"]
                and candidate[0] >= 0.45
            ]
            if not selected:
                selected = [candidates[0]]
            output_ids = [candidate[1].node_id for candidate in selected]
            used.update(output_ids)
            matches[gold["gold_id"]] = output_ids
            best_score, _best_node, _best_parts = candidates[0]
            details.append(
                {
                    "gold_id": gold["gold_id"],
                    "output_node_ids": output_ids,
                    "score": round(best_score, 6),
                    "type_match": any(item[2]["type_match"] for item in selected),
                    "semantic_family_match": any(
                        item[2]["semantic_family_match"] for item in selected
                    ),
                    "text_similarity": max(item[2]["text_similarity"] for item in selected),
                    "geometry_overlap": max(item[2]["geometry_overlap"] for item in selected),
                }
            )
    return matches, details


def evaluate_preprocessing(
    annotation: dict[str, Any],
    nodes: Sequence[DocumentNode],
    matches: dict[str, list[str]],
    alignment: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    gold_nodes = [node for page in annotation["pages"] for node in page["nodes"]]
    indexable = [node for node in gold_nodes if node["indexable"]]
    matched_indexable = [node for node in indexable if node["gold_id"] in matches]
    matched_details = [item for item in alignment if item["output_node_ids"]]
    type_correct = sum(bool(item["type_match"]) for item in matched_details)
    return {
        "gold_node_count": len(gold_nodes),
        "output_node_count": len(nodes),
        "matched_gold_nodes": len(matches),
        "gold_node_recall": _ratio(len(matches), len(gold_nodes)),
        "indexable_gold_node_recall": _ratio(len(matched_indexable), len(indexable)),
        "matched_type_accuracy": _ratio(type_correct, len(matched_details)),
        "mean_text_similarity": _mean(item["text_similarity"] for item in matched_details),
        "text_similarity_interpretation": (
            "diagnostic-only" if annotation.get("text_gold_mode") != "exact" else "scored"
        ),
        "mean_geometry_overlap": _mean(item["geometry_overlap"] for item in matched_details),
        "alignment": list(alignment),
    }


def evaluate_chunking(
    annotation: dict[str, Any],
    chunks: Sequence[ChunkDraft],
    gold_to_nodes: dict[str, list[str]],
) -> dict[str, Any]:
    memberships = [set(chunk.node_ids) | set(chunk.element_node_ids) for chunk in chunks]
    group_results = []
    for group in annotation["chunking"]["must_keep_together"]:
        mapped = [gold_to_nodes.get(gold_id, []) for gold_id in group]
        flattened = {node_id for node_ids in mapped for node_id in node_ids}
        fully_aligned = all(mapped)
        passed = bool(
            fully_aligned and any(flattened.issubset(membership) for membership in memberships)
        )
        group_results.append(
            {
                "gold_ids": group,
                "output_node_ids_by_gold": mapped,
                "fully_aligned": fully_aligned,
                "passed": passed,
            }
        )
    token_counts = [chunk.token_count for chunk in chunks]
    return {
        "chunk_count": len(chunks),
        "modalities": _counts(chunk.modality.value for chunk in chunks),
        "token_counts": token_counts,
        "token_min": min(token_counts) if token_counts else None,
        "token_max": max(token_counts) if token_counts else None,
        "token_mean": _mean(token_counts),
        "multi_page_chunks": sum(chunk.page_start != chunk.page_end for chunk in chunks),
        "must_keep_groups_total": len(group_results),
        "must_keep_groups_passed": sum(item["passed"] for item in group_results),
        "must_keep_group_pass_rate": _ratio(
            sum(item["passed"] for item in group_results), len(group_results)
        ),
        "must_keep_group_results": group_results,
    }


def rank_chunks(
    query_vector: Sequence[float],
    chunk_vectors: Sequence[Sequence[float]],
    *,
    limit: int,
) -> list[tuple[int, float]]:
    scored = [
        (index, _cosine_similarity(query_vector, vector))
        for index, vector in enumerate(chunk_vectors)
    ]
    return sorted(scored, key=lambda item: (-item[1], item[0]))[:limit]


def evaluate_retrieval(
    annotation: dict[str, Any],
    chunks: Sequence[ChunkDraft],
    rankings: dict[str, Sequence[tuple[int, float]]],
    gold_to_nodes: dict[str, list[str]],
) -> dict[str, Any]:
    chunk_memberships = [set(chunk.node_ids) | set(chunk.element_node_ids) for chunk in chunks]
    query_results: list[dict[str, Any]] = []
    reciprocal_ranks: list[float] = []
    recalls: list[float] = []
    for query in annotation["queries"]:
        ranking = rankings.get(query["query_id"], ())
        evidence_nodes = {
            node_id
            for gold_id in query["evidence_gold_ids"]
            for node_id in gold_to_nodes.get(gold_id, [])
        }
        relevant_chunks = {
            index
            for index, membership in enumerate(chunk_memberships)
            if membership & evidence_nodes
        }
        retrieved = [index for index, _score in ranking]
        hits = [index for index in retrieved if index in relevant_chunks]
        recall = (
            len(set(hits)) / len(relevant_chunks)
            if query["answerable"] and relevant_chunks
            else None
        )
        first_rank = next(
            (rank for rank, index in enumerate(retrieved, start=1) if index in relevant_chunks),
            None,
        )
        if recall is not None:
            recalls.append(recall)
            reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
        query_results.append(
            {
                "query_id": query["query_id"],
                "answerable": query["answerable"],
                "evidence_alignment_complete": all(
                    gold_to_nodes.get(gold_id) for gold_id in query["evidence_gold_ids"]
                ),
                "relevant_chunk_ordinals": sorted(relevant_chunks),
                "retrieved": [
                    {"chunk_ordinal": index, "score": round(score, 8)} for index, score in ranking
                ],
                "recall_at_k": round(recall, 6) if recall is not None else None,
                "reciprocal_rank": round(1.0 / first_rank, 6) if first_rank else 0.0,
            }
        )
    return {
        "backend": "evaluation-in-memory-cosine",
        "production_retrieval_service_exercised": False,
        "mean_recall_at_k": _mean(recalls),
        "mrr": _mean(reciprocal_ranks),
        "queries": query_results,
    }


def _node_match_parts(gold: dict[str, Any], node: DocumentNode) -> dict[str, Any]:
    type_match = gold["type"] == node.node_type.value
    semantic_family_match = _same_semantic_family(gold["type"], node.node_type.value)
    geometry = _intersection_over_smaller(gold.get("bounding_box"), node.bounding_box)
    gold_text = _normalize_text(gold.get("text", ""))
    output_text = _normalize_text(node.text)
    if not gold_text and not output_text:
        text_similarity = 1.0
    elif not gold_text or not output_text:
        text_similarity = 0.0
    else:
        text_similarity = SequenceMatcher(None, gold_text, output_text).ratio()
    type_score = 0.4 if type_match else (0.2 if semantic_family_match else 0.0)
    score = type_score + 0.3 * geometry + 0.3 * text_similarity
    return {
        "score": round(score, 6),
        "type_match": type_match,
        "semantic_family_match": semantic_family_match,
        "text_similarity": round(text_similarity, 6),
        "geometry_overlap": round(geometry, 6),
    }


def _intersection_over_smaller(
    gold_box: Sequence[float] | None, output_box: Sequence[float]
) -> float:
    if gold_box is None or len(gold_box) != 4:
        return 0.0
    x0, y0 = max(gold_box[0], output_box[0]), max(gold_box[1], output_box[1])
    x1, y1 = min(gold_box[2], output_box[2]), min(gold_box[3], output_box[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    intersection = (x1 - x0) * (y1 - y0)
    gold_area = (gold_box[2] - gold_box[0]) * (gold_box[3] - gold_box[1])
    output_area = (output_box[2] - output_box[0]) * (output_box[3] - output_box[1])
    return intersection / max(min(gold_area, output_area), 1.0)


def _cosine_similarity(first: Sequence[float], second: Sequence[float]) -> float:
    if len(first) != len(second) or not first:
        raise ValueError("向量维度必须相同且非空")
    numerator = sum(a * b for a, b in zip(first, second, strict=True))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if first_norm == 0 or second_norm == 0:
        return 0.0
    return numerator / (first_norm * second_norm)


def _normalize_text(value: str) -> str:
    return _SPACE.sub(" ", value.casefold()).strip()


def _same_semantic_family(first: str, second: str) -> bool:
    textual = {"heading", "paragraph", "list", "caption", "code"}
    visual = {"image", "chart"}
    return (first in textual and second in textual) or (first in visual and second in visual)


def _counts(values: Sequence[str] | Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def _mean(values: Any) -> float | None:
    normalized = list(values)
    return round(sum(normalized) / len(normalized), 6) if normalized else None


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0
