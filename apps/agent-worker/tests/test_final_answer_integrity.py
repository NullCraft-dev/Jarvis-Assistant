from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.answer_constraints import ExplicitAnswerConstraintValidator
from jarvis_worker.agent.core.final_answer_integrity import (
    CitationVerdictConsistencyValidator,
    FinalMessageIntegrityValidator,
)
from jarvis_worker.agent.core.state import AgentState


def test_final_message_integrity_rejects_truncated_conjunction() -> None:
    result = FinalMessageIntegrityValidator().validate(
        action=AgentAction.finish("已选文档中没有足够证据；我不会推测数值，也不"),
        state=AgentState(user_goal="回答问题"),
    )

    assert result.accepted is False
    assert result.reason_code == "FINAL_MESSAGE_INCOMPLETE"


def test_final_message_integrity_accepts_complete_no_evidence_answer() -> None:
    result = FinalMessageIntegrityValidator().validate(
        action=AgentAction.finish("已选文档中没有足够证据；我不会推测数值，也不会引用无关段落。"),
        state=AgentState(user_goal="回答问题"),
    )

    assert result.accepted is True


def test_final_message_integrity_buffers_rag_but_not_plain_chat() -> None:
    validator = FinalMessageIntegrityValidator()

    assert validator.requires_buffered_output(AgentState(user_goal="你好")) is False
    assert (
        validator.requires_buffered_output(
            AgentState(
                user_goal="根据文档回答",
                intent={"retrieval": {"mode": "required"}},
            )
        )
        is True
    )


def test_citation_verdict_rejects_contradictory_support_claim() -> None:
    state = AgentState(user_goal="你上一条回答的第二个引用真的支持前一句吗？重新核对原文。")
    result = CitationVerdictConsistencyValidator().validate(
        action=AgentAction.finish(
            "你的质疑成立，这个引用张冠李戴，需要纠正；但该引用正是支持前一句的直接证据，这一点成立。"
        ),
        state=state,
    )

    assert result.accepted is False
    assert result.reason_code == "CITATION_VERDICT_CONTRADICTORY"


def test_citation_verdict_accepts_one_unambiguous_verdict() -> None:
    state = AgentState(user_goal="你上一条回答的第二个引用真的支持前一句吗？重新核对原文。")
    result = CitationVerdictConsistencyValidator().validate(
        action=AgentAction.finish("支持。重新核对后，该引用直接支持前一句，无需更换引用。"),
        state=state,
    )

    assert result.accepted is True
    assert result.metadata["citation_verdict"] == "supported"


def test_citation_verdict_accepts_unambiguous_rejection() -> None:
    state = AgentState(user_goal="请核对上一轮回答的引用是否支持结论。")
    result = CitationVerdictConsistencyValidator().validate(
        action=AgentAction.finish("核对原文后，该引用不支持上一轮的结论。"),
        state=state,
    )

    assert result.accepted is True
    assert result.metadata["citation_verdict"] == "unsupported"


def test_explicit_answer_constraints_reject_length_overflow() -> None:
    state = AgentState(user_goal="请给出不超过 20 字的总结。")
    result = ExplicitAnswerConstraintValidator().validate(
        action=AgentAction.finish("这是一个明显超过二十个字符限制的最终回答内容。"),
        state=state,
    )

    assert result.accepted is False
    assert result.reason_code == "ANSWER_LENGTH_LIMIT_EXCEEDED"


def test_explicit_answer_constraints_host_clamp_second_overflow() -> None:
    state = AgentState(
        user_goal="请给出不超过 20 字的总结。",
        answer_guard_rejections=1,
    )

    result = ExplicitAnswerConstraintValidator().validate(
        action=AgentAction.finish("这是第二次仍然明显超过二十个字符限制的最终回答内容。"),
        state=state,
    )

    assert result.accepted is True
    assert len("".join(result.output.split())) <= 20
    assert result.metadata["host_normalized"] is True


def test_explicit_answer_constraints_host_clamp_preserves_bounded_disclosure() -> None:
    state = AgentState(
        user_goal="找出所有相关章节，并给出不超过 80 字的总结。",
        observations=[{"tool_name": "rag.search", "ok": True}],
        answer_guard_rejections=1,
    )

    result = ExplicitAnswerConstraintValidator().validate(
        action=AgentAction.finish("章节一。" * 80),
        state=state,
    )

    assert result.accepted is True
    assert len("".join(result.output.split())) <= 80
    assert "本次有界检索" in result.output
    assert "仍可能遗漏" in result.output


def test_explicit_answer_constraints_require_bounded_recall_disclosure() -> None:
    state = AgentState(
        user_goal="找出所有直接提到 verification 和 validation 区别的章节。",
        observations=[{"tool_name": "rag.search", "ok": True}],
    )
    validator = ExplicitAnswerConstraintValidator()

    rejected = validator.validate(
        action=AgentAction.finish("以下是全部相关章节，没有遗漏。"),
        state=state,
    )
    accepted = validator.validate(
        action=AgentAction.finish("基于本次有界检索，命中了以下章节；仍可能存在未召回内容。"),
        state=state,
    )

    assert rejected.reason_code == "BOUNDED_RETRIEVAL_DISCLOSURE_MISSING"
    assert accepted.accepted is True


def test_explicit_answer_constraints_require_fact_inference_labels() -> None:
    state = AgentState(user_goal="明确区分原文事实和你的判断。")
    validator = ExplicitAnswerConstraintValidator()

    rejected = validator.validate(
        action=AgentAction.finish("论文没有讨论部署成本，我认为这是缺口。"),
        state=state,
    )
    accepted = validator.validate(
        action=AgentAction.finish("原文事实：论文未讨论部署成本。我的判断：这是重要缺口。"),
        state=state,
    )

    assert rejected.reason_code == "FACT_INFERENCE_BOUNDARY_MISSING"
    assert accepted.accepted is True
