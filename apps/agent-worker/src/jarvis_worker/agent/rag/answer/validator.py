"""用可信 `rag.search` ToolResult 校验和渲染模型引用。"""

from __future__ import annotations

import re
from typing import Any

from jarvis_worker.agent.context.response_language import (
    ResponseLanguage,
    resolve_response_language_policy,
)
from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.conversation_constraints import (
    is_prior_answer_transform_goal,
)
from jarvis_worker.agent.core.final_answer import FinalAnswerValidation
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.rag.answer.contracts import RagAnswer, RagCitation
from jarvis_worker.agent.rag.evidence import (
    RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
    RAG_EVIDENCE_ASSESSMENT_SCHEMA,
    trusted_rag_chunk_evidence,
)

_MAX_CITATIONS = 12
_MAX_EXCERPT_CHARS = 280
_MODEL_AUTHORED_CITATION_SECTION = re.compile(
    r"(?:\A|\n)\s*(?:#{1,6}\s*)?"
    r"(?:引用|参考资料|参考文献|sources?|references?)\s*[：:]\s*\n"
    r"\s*(?:[-*+]\s+(?:\[\d+\]\s*)?|\[\d+\]\s+)",
    re.IGNORECASE,
)
_LATIN_PAGE_REFERENCE = re.compile(
    r"\bpp?\.\s*(\d{1,5})(?:\s*[-–—~]\s*(\d{1,5}))?",
    re.IGNORECASE,
)
_CHINESE_PAGE_REFERENCE = re.compile(r"第\s*(\d{1,5})\s*(?:[-–—~至]\s*(\d{1,5})\s*)?页")

_REASON_CITATION_FORMAT = "RAG_CITATION_FORMAT_INVALID"
_REASON_CITATION_MISSING = "RAG_CITATION_MISSING"
_REASON_CITATION_DUPLICATE = "RAG_CITATION_DUPLICATE"
_REASON_CITATION_UNTRUSTED = "RAG_CITATION_UNTRUSTED"
_REASON_CITATION_BODY_MISSING = "RAG_CITATION_BODY_MISSING"


class RagCitationValidator:
    validator_id = "rag-citation-v1"

    def requires_buffered_output(self, state: AgentState) -> bool:
        return _latest_answer_evidence_tool(state.observations) == "rag.search" or (
            is_prior_answer_transform_goal(state.user_goal)
            and bool(_trusted_history_rag_chunk_ids(state))
        )

    def validate(self, *, action: AgentAction, state: AgentState) -> FinalAnswerValidation:
        observations = _rag_observations(state.observations)
        if not observations:
            if is_prior_answer_transform_goal(state.user_goal):
                historical = _trusted_history_rag_chunk_ids(state)
                if historical:
                    requested = _requested_chunk_ids(action)
                    if requested is None or any(item not in historical for item in requested):
                        return _rejected(
                            "历史改写只能沿用最近完整历史 Run 的可信 RAG chunk_id",
                            reason_code=_REASON_CITATION_UNTRUSTED,
                        )
                    answer_body = _without_model_authored_citation_section(action.final_message)
                    if not answer_body:
                        return _rejected(
                            "历史改写不能只包含模型自行编写的引用列表",
                            reason_code=_REASON_CITATION_BODY_MISSING,
                        )
                    return FinalAnswerValidation(
                        accepted=True,
                        output=answer_body,
                        metadata={
                            "validator": self.validator_id,
                            "evidence_mode": "trusted_history_transform",
                            "historical_citation_ids": list(requested or ()),
                            "rag_citations_used": bool(requested),
                        },
                    )
            if action.citations:
                return _rejected(
                    "没有成功的 rag.search 结果，不能声明 RAG 引用",
                    reason_code=_REASON_CITATION_UNTRUSTED,
                )
            return FinalAnswerValidation(accepted=True, output=action.final_message)

        # A Run may use RAG for broad discovery and then verify the answer against a
        # newer, direct Workspace source.  In that case the Workspace evidence chain
        # owns the uncited final answer; merely having an earlier rag.search result
        # must not keep the answer permanently bound to RAG citation semantics.
        # Explicit citations still opt into strict RAG validation, including forged
        # citation rejection.
        latest_evidence_tool = _latest_answer_evidence_tool(state.observations)
        if (
            latest_evidence_tool in _WORKSPACE_DIRECT_EVIDENCE_TOOLS
            and not action.citations
            and not action.insufficient_evidence
        ):
            return FinalAnswerValidation(
                accepted=True,
                output=action.final_message,
                metadata={
                    "validator": self.validator_id,
                    "evidence_mode": "workspace_direct",
                    "rag_citations_used": False,
                },
            )

        evidence: dict[str, RagCitation] = {}
        for observation in observations:
            evidence.update(_trusted_evidence(observation))
        sufficiency = _latest_evidence_assessment(observations)
        if sufficiency is not None and sufficiency.get("sufficient") is not True:
            reason_code = str(sufficiency.get("reason_code", "UNKNOWN"))
            answer = RagAnswer(
                answer=_host_insufficient_evidence_message(state),
                insufficient_evidence=True,
            )
            metadata = _answer_metadata(answer)
            metadata.update(
                {
                    "safe_degradation": "host_owned",
                    "evidence_reason_code": reason_code[:100],
                }
            )
            return FinalAnswerValidation(
                accepted=True,
                output=answer.answer,
                metadata=metadata,
            )
        requested_values: list[str] = []
        for citation in action.citations:
            if (
                not isinstance(citation, dict)
                or set(citation) != {"chunk_id"}
                or not isinstance(citation.get("chunk_id"), str)
                or not citation["chunk_id"].strip()
            ):
                return _rejected(
                    "citation 必须且只能包含非空字符串 chunk_id",
                    evidence=evidence,
                    reason_code=_REASON_CITATION_FORMAT,
                )
            requested_values.append(citation["chunk_id"].strip())
        requested = tuple(requested_values)
        resolved_from_page_references = False
        if action.insufficient_evidence:
            if requested:
                return _rejected(
                    "insufficient_evidence=true 时 citations 必须为空",
                    evidence=evidence,
                    reason_code=_REASON_CITATION_FORMAT,
                )
            answer = RagAnswer(
                answer=action.final_message,
                insufficient_evidence=True,
            )
            return FinalAnswerValidation(
                accepted=True,
                output=answer.answer,
                metadata=_answer_metadata(answer),
            )
        if not evidence:
            return _rejected(
                "rag.search 没有返回证据；必须设置 insufficient_evidence=true 且 citations=[]",
                reason_code=_REASON_CITATION_MISSING,
            )
        if not requested:
            requested = _citations_from_explicit_page_references(
                action.final_message,
                evidence,
            )
            resolved_from_page_references = bool(requested)
        if not requested:
            return _rejected(
                "基于 RAG 证据回答时至少需要一个 citations.chunk_id",
                evidence=evidence,
                reason_code=_REASON_CITATION_MISSING,
            )
        if len(requested) > _MAX_CITATIONS or len(set(requested)) != len(requested):
            return _rejected(
                "citations 必须唯一且不超过 12 项",
                evidence=evidence,
                reason_code=_REASON_CITATION_DUPLICATE,
            )

        citations: list[RagCitation] = []
        for raw_chunk_id in requested:
            record = evidence.get(raw_chunk_id)
            if record is None:
                return _rejected(
                    f"引用 {raw_chunk_id} 不在当前 rag.search 的可信结果中",
                    evidence=evidence,
                    reason_code=_REASON_CITATION_UNTRUSTED,
                )
            citations.append(record)

        # 模型可能为了响应“给出页码引用”而在正文末尾再次撰写 Sources/引用列表。
        # 该列表中的标题、页码和 chunk 身份均不是可信 owner，不能直接展示；但在顶层
        # citations 已通过当前 Run ToolResult 校验时，也没有必要让整条任务失败。这里丢弃
        # 模型自写的尾部引用区，只保留回答正文，随后统一渲染 Runtime 拥有的可信引用。
        answer_body = _without_model_authored_citation_section(action.final_message)
        if not answer_body:
            return _rejected(
                "final_message 不能只包含模型自行编写的引用列表；必须提供回答正文",
                evidence=evidence,
                reason_code=_REASON_CITATION_BODY_MISSING,
            )
        answer = RagAnswer(
            answer=answer_body,
            citations=tuple(citations),
            insufficient_evidence=False,
        )
        metadata = _answer_metadata(answer)
        if resolved_from_page_references:
            metadata["citation_resolution"] = "explicit_page_reference"
        return FinalAnswerValidation(
            accepted=True,
            output=_render(answer),
            metadata=metadata,
        )


def _requested_chunk_ids(action: AgentAction) -> tuple[str, ...] | None:
    requested: list[str] = []
    for citation in action.citations:
        if (
            not isinstance(citation, dict)
            or set(citation) != {"chunk_id"}
            or not isinstance(citation.get("chunk_id"), str)
            or not citation["chunk_id"].strip()
        ):
            return None
        value = citation["chunk_id"].strip()
        if value in requested or len(requested) >= _MAX_CITATIONS:
            return None
        requested.append(value)
    return tuple(requested)


def _trusted_history_rag_chunk_ids(state: AgentState) -> frozenset[str]:
    return frozenset(
        value
        for item in state.trusted_history_provenance
        if isinstance(item, dict) and isinstance((value := item.get("rag_chunk_id")), str) and value
    )


def _rag_observations(
    observations: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    trusted = [
        observation
        for observation in observations
        if (
            isinstance(observation, dict)
            and observation.get("tool_name") == "rag.search"
            and observation.get("ok") is True
        )
    ]
    return tuple(trusted[-4:])


_WORKSPACE_DIRECT_EVIDENCE_TOOLS = frozenset({"workspace.read_file", "workspace.read_files"})
_ANSWER_EVIDENCE_TOOLS = _WORKSPACE_DIRECT_EVIDENCE_TOOLS | {"rag.search"}


def _latest_answer_evidence_tool(observations: list[dict[str, Any]]) -> str:
    """Return the latest successful tool that can directly support answer claims."""
    for observation in reversed(observations):
        if not isinstance(observation, dict) or observation.get("ok") is not True:
            continue
        tool_name = observation.get("tool_name")
        if isinstance(tool_name, str) and tool_name in _ANSWER_EVIDENCE_TOOLS:
            return tool_name
    return ""


def _trusted_evidence(observation: dict[str, Any]) -> dict[str, RagCitation]:
    evidence: dict[str, RagCitation] = {}
    for item in trusted_rag_chunk_evidence(observation):
        evidence[str(item.chunk_id)] = RagCitation(
            chunk_id=item.chunk_id,
            document_id=item.document_id,
            document_title=item.document_title,
            source_artifact_id=item.source_artifact_id,
            source_locator=item.source_locator,
            evidence_excerpt=item.content.strip()[:_MAX_EXCERPT_CHARS],
        )
    return evidence


def _latest_evidence_assessment(
    observations: tuple[dict[str, Any], ...],
) -> dict[str, Any] | None:
    if not observations:
        return None
    # Sufficiency belongs to the latest retrieval, never to an older observation
    # whose query/scope may differ.  Missing and pre-v2 projections fail closed so
    # an upgrade cannot resume an old checkpoint around the current policy.
    data = observations[-1].get("data")
    if not isinstance(data, dict):
        return _invalid_assessment("EVIDENCE_ASSESSMENT_MISSING")
    assessment = data.get("evidence_assessment")
    if not isinstance(assessment, dict):
        return _invalid_assessment("EVIDENCE_ASSESSMENT_MISSING")
    if assessment.get("schema") != RAG_EVIDENCE_ASSESSMENT_SCHEMA or not isinstance(
        assessment.get("sufficient"), bool
    ):
        return _invalid_assessment("EVIDENCE_ASSESSMENT_INVALID")
    if assessment.get("policy_version") != RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION:
        return _invalid_assessment("EVIDENCE_ASSESSMENT_POLICY_UNSUPPORTED")
    return assessment


def _invalid_assessment(reason_code: str) -> dict[str, Any]:
    return {
        "schema": RAG_EVIDENCE_ASSESSMENT_SCHEMA,
        "policy_version": RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
        "sufficient": False,
        "reason_code": reason_code,
    }


def _host_insufficient_evidence_message(state: AgentState) -> str:
    policy = resolve_response_language_policy(state.memory_items, state.user_goal)
    use_english = policy is not None and policy.effective_language is ResponseLanguage.EN
    if policy is None:
        use_english = not bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", state.user_goal))
    if use_english:
        return (
            "The selected documents do not contain enough relevant evidence to answer this "
            "question. I will not infer a value or cite unrelated passages."
        )
    return "已选文档中没有足够的相关证据回答这个问题；我不会推测数值，也不会引用无关段落。"


def _render(answer: RagAnswer) -> str:
    lines = [answer.answer.rstrip(), "", "引用："]
    for index, citation in enumerate(answer.citations, 1):
        location = _location(citation.source_locator)
        suffix = f" · {location}" if location else ""
        href = f"/knowledge/rag?document_id={citation.document_id}&chunk_id={citation.chunk_id}"
        title = _escape_markdown_text(citation.document_title)
        lines.append(f"- [引用 {index}]({href}) · {title}{suffix}")
    return "\n".join(lines)


def _escape_markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_[\]{}()#+.!|>-])", r"\\\1", value)


def _location(locator: dict) -> str:
    page = locator.get("page_start") or locator.get("page_number")
    heading = locator.get("heading_path")
    parts: list[str] = []
    if isinstance(page, int) and page > 0:
        parts.append(f"p.{page}")
    if isinstance(heading, list):
        clean = [value for item in heading if (value := _clean_heading_label(item)) is not None]
        if clean:
            parts.append(" / ".join(clean[:4]))
    return " · ".join(parts)


def _clean_heading_label(value: object) -> str | None:
    """Drop parser navigation/OCR noise from user-visible citation labels."""

    label = re.sub(r"\s+", " ", str(value)).strip()
    if not label or len(label) > 180:
        return None
    tokens = label.split()
    numeric = sum(bool(re.fullmatch(r"[#\d.,;:()\[\]-]+", token)) for token in tokens)
    if len(tokens) >= 10 and numeric / len(tokens) >= 0.5:
        return None
    return label


def _answer_metadata(answer: RagAnswer) -> dict[str, Any]:
    return {
        "validator": RagCitationValidator.validator_id,
        "insufficient_evidence": answer.insufficient_evidence,
        "citations": [
            {
                "chunk_id": str(citation.chunk_id),
                "document_id": str(citation.document_id),
                "document_title": citation.document_title,
                "source_artifact_id": str(citation.source_artifact_id),
                "source_locator": citation.source_locator,
                "evidence_excerpt": citation.evidence_excerpt,
            }
            for citation in answer.citations
        ],
    }


def _without_model_authored_citation_section(message: str) -> str:
    match = _MODEL_AUTHORED_CITATION_SECTION.search(message)
    if match is None:
        return message
    return message[: match.start()].rstrip()


def _citations_from_explicit_page_references(
    message: str,
    evidence: dict[str, RagCitation],
) -> tuple[str, ...]:
    """把模型正文中的显式页码引用映射回当前 Run 的可信 chunk 身份。"""
    pages: set[int] = set()
    for pattern in (_LATIN_PAGE_REFERENCE, _CHINESE_PAGE_REFERENCE):
        for match in pattern.finditer(message):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else start
            if start <= 0 or end < start or end - start > 20:
                continue
            pages.update(range(start, end + 1))
    if not pages:
        return ()

    resolved: list[str] = []
    for chunk_id, citation in evidence.items():
        locator = citation.source_locator
        start = locator.get("page_start") or locator.get("page_number")
        end = locator.get("page_end") or start
        if not (isinstance(start, int) and isinstance(end, int) and start > 0 and end >= start):
            continue
        if any(start <= page <= end for page in pages):
            resolved.append(chunk_id)
            if len(resolved) >= _MAX_CITATIONS:
                break
    return tuple(resolved)


def _allowed_citation_manifest(evidence: dict[str, RagCitation]) -> str:
    if not evidence:
        return ""
    entries: list[str] = []
    for chunk_id, citation in list(evidence.items())[:_MAX_CITATIONS]:
        page = citation.source_locator.get("page_start") or citation.source_locator.get(
            "page_number"
        )
        suffix = f"（p.{page}）" if isinstance(page, int) and page > 0 else ""
        entries.append(f"{chunk_id}{suffix}")
    return (
        " 本轮允许的 citations.chunk_id 只能从以下动态检索证据中选择：" + "；".join(entries) + "。"
    )


def _rejected(
    feedback: str,
    *,
    evidence: dict[str, RagCitation] | None = None,
    reason_code: str = "FINAL_ANSWER_REJECTED",
) -> FinalAnswerValidation:
    return FinalAnswerValidation(
        accepted=False,
        output="",
        feedback=(
            "RAG 引用校验失败："
            + feedback
            + "。请只引用当前 Run 成功 rag.search 返回的 chunk_id。"
            + _allowed_citation_manifest(evidence or {})
            + " final_message 只写回答正文，不要自行添加引用、参考资料、Sources 或 References 列表；"
            "Runtime 会根据 citations 统一展示可信标题和页码。"
        ),
        reason_code=reason_code,
    )
