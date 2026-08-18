from __future__ import annotations

from uuid import uuid4

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.rag.answer import RagCitationValidator
from jarvis_worker.agent.rag.evidence import (
    RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
    RAG_EVIDENCE_ASSESSMENT_SCHEMA,
)


def _observation(*, with_results: bool = True):
    chunk_id, document_id, artifact_id = uuid4(), uuid4(), uuid4()
    results = (
        [
            {
                "chunk_id": str(chunk_id),
                "document_id": str(document_id),
                "document_title": "Trusted paper",
                "source_artifact_id": str(artifact_id),
                "chunks": [
                    {
                        "chunk_id": str(chunk_id),
                        "role": "primary",
                        "content": "Trusted evidence from the indexed document.",
                        "source_locator": {
                            "page_start": 7,
                            "heading_path": ["Results"],
                        },
                    }
                ],
                "elements": [],
            }
        ]
        if with_results
        else []
    )
    return (
        {
            "tool_call_id": "rag-call",
            "tool_name": "rag.search",
            "model_action": {
                "action_type": "call_tool",
                "tool_name": "rag.search",
                "arguments": {"query": "question"},
                "reason": "需要文档证据",
            },
            "ok": True,
            "summary": "RAG 检索完成",
            "data": {
                "query": "question",
                "results": results,
                "truncated": False,
                "evidence_assessment": {
                    "schema": RAG_EVIDENCE_ASSESSMENT_SCHEMA,
                    "policy_version": RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
                    "sufficient": with_results,
                    "reason_code": "SUFFICIENT" if with_results else "NO_EVIDENCE",
                },
            },
        },
        chunk_id,
    )


def _workspace_observation(tool_name: str = "workspace.read_file"):
    return {
        "tool_call_id": "workspace-read",
        "tool_name": tool_name,
        "model_action": {
            "action_type": "call_tool",
            "tool_name": tool_name,
            "arguments": {"path": "procurement/policy.md"},
            "reason": "读取权威原文",
        },
        "ok": True,
        "summary": "Read file: procurement/policy.md",
        "data": {"path": "procurement/policy.md", "content": "唯一真源"},
    }


def test_citation_validator_renders_only_trusted_source_metadata():
    observation, chunk_id = _observation()
    state = AgentState(observations=[observation])
    action = AgentAction.finish(
        "结论来自检索证据。",
        citations=({"chunk_id": str(chunk_id)},),
    )

    result = RagCitationValidator().validate(action=action, state=state)

    assert result.accepted is True
    assert "Trusted paper" in result.output
    assert "p.7" in result.output
    assert f"chunk_id={chunk_id}" in result.output
    assert "/knowledge/rag?document_id=" in result.output
    assert "[引用 1]" in result.output
    assert result.metadata["citations"][0]["document_title"] == "Trusted paper"


def test_citation_renderer_drops_numeric_ocr_navigation_heading():
    observation, chunk_id = _observation()
    observation["data"]["results"][0]["chunks"][0]["source_locator"]["heading_path"] = [
        "# " + " ".join(str(value) for value in range(61, 400)),
        "2.4 Distinctions between Product Verification and Product Validation",
    ]
    state = AgentState(observations=[observation])
    action = AgentAction.finish(
        "结论来自检索证据。",
        citations=({"chunk_id": str(chunk_id)},),
    )

    result = RagCitationValidator().validate(action=action, state=state)

    assert result.accepted is True
    assert "61 62 63" not in result.output
    assert "2.4 Distinctions" in result.output


def test_citation_validator_rejects_forged_chunk_id():
    observation, _chunk_id = _observation()
    state = AgentState(observations=[observation])
    action = AgentAction.finish(
        "伪造引用",
        citations=({"chunk_id": str(uuid4())},),
    )

    result = RagCitationValidator().validate(action=action, state=state)

    assert result.accepted is False
    assert "不在当前 rag.search" in result.feedback


def test_citation_validator_accepts_trusted_history_citations_for_pure_transform():
    chunk_id = uuid4()
    state = AgentState(
        user_goal="把刚才的比较结果压缩成四行表格，不要新增内容。",
        trusted_history_provenance=[{"rag_chunk_id": str(chunk_id)}],
    )
    action = AgentAction.finish(
        "| 主题 | NIST | NASA | 差异 |\n|---|---|---|---|\n| 风险 | 治理 | 工程 | 范围不同 |",
        citations=({"chunk_id": str(chunk_id)},),
    )
    validator = RagCitationValidator()

    result = validator.validate(action=action, state=state)

    assert validator.requires_buffered_output(state) is True
    assert result.accepted is True
    assert result.metadata["evidence_mode"] == "trusted_history_transform"
    assert result.metadata["historical_citation_ids"] == [str(chunk_id)]


def test_citation_validator_rejects_untrusted_history_citation_for_transform():
    state = AgentState(
        user_goal="把刚才的比较结果压缩成四行表格，不要新增内容。",
        trusted_history_provenance=[{"rag_chunk_id": str(uuid4())}],
    )

    result = RagCitationValidator().validate(
        action=AgentAction.finish("压缩结果", citations=({"chunk_id": str(uuid4())},)),
        state=state,
    )

    assert result.accepted is False
    assert result.reason_code == "RAG_CITATION_UNTRUSTED"


def test_citation_validator_discards_model_authored_citation_section():
    observation, chunk_id = _observation()
    state = AgentState(observations=[observation])
    action = AgentAction.finish(
        f"结论来自检索证据。\n\n引用：\n- [1] Trusted paper · p.7 (`chunk:{chunk_id}`)",
        citations=({"chunk_id": str(chunk_id)},),
    )

    result = RagCitationValidator().validate(action=action, state=state)

    assert result.accepted is True
    assert result.output.startswith("结论来自检索证据。\n\n引用：\n")
    assert result.output.count("引用：") == 1
    assert result.output.count("Trusted paper") == 1
    assert "p.7" in result.output


def test_citation_validator_rejects_citation_only_body_after_discard():
    observation, chunk_id = _observation()
    state = AgentState(observations=[observation])
    action = AgentAction.finish(
        f"引用：\n- [1] Trusted paper · p.7 (`chunk:{chunk_id}`)",
        citations=({"chunk_id": str(chunk_id)},),
    )

    result = RagCitationValidator().validate(action=action, state=state)

    assert result.accepted is False
    assert result.reason_code == "RAG_CITATION_BODY_MISSING"


def test_citation_validator_rejection_lists_only_dynamic_trusted_ids():
    observation, chunk_id = _observation()
    forged_id = uuid4()
    state = AgentState(observations=[observation])
    action = AgentAction.finish(
        "结论来自检索证据。",
        citations=({"chunk_id": str(forged_id)},),
    )

    result = RagCitationValidator().validate(action=action, state=state)

    assert result.accepted is False
    assert result.reason_code == "RAG_CITATION_UNTRUSTED"
    assert str(chunk_id) in result.feedback
    assert str(forged_id) in result.feedback
    assert "p.7" in result.feedback


def test_citation_validator_maps_explicit_chinese_page_reference_to_trusted_chunk():
    observation, chunk_id = _observation()
    state = AgentState(observations=[observation])

    result = RagCitationValidator().validate(
        action=AgentAction.finish("作者使用两阶段方法评估检索质量（见第 7 页）。"),
        state=state,
    )

    assert result.accepted is True
    assert f"chunk_id={chunk_id}" in result.output
    assert result.metadata["citation_resolution"] == "explicit_page_reference"


def test_citation_validator_does_not_map_unretrieved_page_reference():
    observation, _chunk_id = _observation()
    state = AgentState(observations=[observation])

    result = RagCitationValidator().validate(
        action=AgentAction.finish("作者使用两阶段方法评估检索质量（见 p.999）。"),
        state=state,
    )

    assert result.accepted is False
    assert result.reason_code == "RAG_CITATION_MISSING"


def test_citation_validator_accepts_explicit_insufficient_evidence():
    observation, _chunk_id = _observation(with_results=False)
    state = AgentState(user_goal="回答当前文档的问题", observations=[observation])
    action = AgentAction.finish(
        "当前文档中没有足够证据。",
        insufficient_evidence=True,
    )

    result = RagCitationValidator().validate(action=action, state=state)

    assert result.accepted is True
    assert result.output == (
        "已选文档中没有足够的相关证据回答这个问题；我不会推测数值，也不会引用无关段落。"
    )
    assert result.metadata["insufficient_evidence"] is True
    assert result.metadata["safe_degradation"] == "host_owned"


def test_citation_validator_safely_degrades_when_document_coverage_is_incomplete():
    observation, chunk_id = _observation()
    observation["data"]["evidence_assessment"] = {
        "schema": RAG_EVIDENCE_ASSESSMENT_SCHEMA,
        "policy_version": RAG_EVIDENCE_ASSESSMENT_POLICY_VERSION,
        "sufficient": False,
        "reason_code": "REQUESTED_DOCUMENT_COVERAGE_INCOMPLETE",
        "evidence_count": 1,
        "covered_document_count": 1,
        "requested_document_count": 2,
    }
    state = AgentState(user_goal="比较两份文档", observations=[observation])

    result = RagCitationValidator().validate(
        action=AgentAction.finish(
            "只覆盖一份文档却声称比较完成。",
            citations=({"chunk_id": str(chunk_id)},),
        ),
        state=state,
    )

    assert result.accepted is True
    assert result.output == (
        "已选文档中没有足够的相关证据回答这个问题；我不会推测数值，也不会引用无关段落。"
    )
    assert result.metadata["insufficient_evidence"] is True
    assert result.metadata["citations"] == []
    assert result.metadata["safe_degradation"] == "host_owned"
    assert result.metadata["evidence_reason_code"] == ("REQUESTED_DOCUMENT_COVERAGE_INCOMPLETE")


def test_citation_validator_fails_closed_when_latest_assessment_is_missing():
    observation, chunk_id = _observation()
    observation["data"].pop("evidence_assessment")
    state = AgentState(user_goal="根据文档回答", observations=[observation])

    result = RagCitationValidator().validate(
        action=AgentAction.finish(
            "模型声称证据充分。",
            citations=({"chunk_id": str(chunk_id)},),
        ),
        state=state,
    )

    assert result.accepted is True
    assert result.metadata["insufficient_evidence"] is True
    assert result.metadata["evidence_reason_code"] == "EVIDENCE_ASSESSMENT_MISSING"


def test_citation_validator_fails_closed_for_pre_v2_assessment():
    observation, chunk_id = _observation()
    observation["data"]["evidence_assessment"].pop("policy_version")
    state = AgentState(user_goal="根据文档回答", observations=[observation])

    result = RagCitationValidator().validate(
        action=AgentAction.finish(
            "模型声称证据充分。",
            citations=({"chunk_id": str(chunk_id)},),
        ),
        state=state,
    )

    assert result.accepted is True
    assert result.metadata["evidence_reason_code"] == ("EVIDENCE_ASSESSMENT_POLICY_UNSUPPORTED")


def test_citation_validator_never_uses_older_assessment_for_latest_retrieval():
    older, _chunk_id = _observation()
    latest, latest_chunk_id = _observation()
    latest["data"].pop("evidence_assessment")
    state = AgentState(user_goal="根据最新检索回答", observations=[older, latest])

    result = RagCitationValidator().validate(
        action=AgentAction.finish(
            "模型引用最新结果。",
            citations=({"chunk_id": str(latest_chunk_id)},),
        ),
        state=state,
    )

    assert result.accepted is True
    assert result.metadata["evidence_reason_code"] == "EVIDENCE_ASSESSMENT_MISSING"


def test_citation_validator_accepts_explicit_degradation_for_incomplete_coverage():
    observation, _chunk_id = _observation()
    observation["data"]["document_coverage"] = {
        "requested_count": 2,
        "covered_count": 1,
        "complete": False,
        "uncovered_document_ids": [str(uuid4())],
    }
    state = AgentState(observations=[observation])

    result = RagCitationValidator().validate(
        action=AgentAction.finish(
            "当前只检索到一份指定资料，无法完成全面比较。",
            insufficient_evidence=True,
        ),
        state=state,
    )

    assert result.accepted is True
    assert result.metadata["insufficient_evidence"] is True


def test_citation_validator_requires_citation_when_evidence_is_used():
    observation, _chunk_id = _observation()
    state = AgentState(observations=[observation])

    result = RagCitationValidator().validate(
        action=AgentAction.finish("没有结构化引用"),
        state=state,
    )

    assert result.accepted is False
    assert "至少需要一个" in result.feedback


def test_citation_validator_allows_newer_direct_workspace_evidence_without_rag_citation():
    observation, _chunk_id = _observation()
    state = AgentState(observations=[observation, _workspace_observation()])

    result = RagCitationValidator().validate(
        action=AgentAction.finish("结论来自随后读取的工作区权威原文。"),
        state=state,
    )

    assert result.accepted is True
    assert result.output == "结论来自随后读取的工作区权威原文。"
    assert result.metadata["evidence_mode"] == "workspace_direct"
    assert result.metadata["rag_citations_used"] is False


def test_citation_validator_still_requires_citation_when_rag_is_latest_evidence():
    observation, _chunk_id = _observation()
    state = AgentState(observations=[_workspace_observation(), observation])

    result = RagCitationValidator().validate(
        action=AgentAction.finish("没有结构化引用"),
        state=state,
    )

    assert result.accepted is False
    assert result.reason_code == "RAG_CITATION_MISSING"


def test_citation_validator_rejects_forged_citation_after_workspace_read():
    observation, _chunk_id = _observation()
    state = AgentState(observations=[observation, _workspace_observation()])

    result = RagCitationValidator().validate(
        action=AgentAction.finish("仍声明使用 RAG 引用", citations=({"chunk_id": str(uuid4())},)),
        state=state,
    )

    assert result.accepted is False
    assert result.reason_code == "RAG_CITATION_UNTRUSTED"
