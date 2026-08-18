from jarvis_worker.agent.context.response_language import (
    ResponseLanguage,
    ResponseLanguagePreferenceValidator,
    resolve_response_language_policy,
)
from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.state import AgentState


def _memory(
    content: str,
    *,
    scope: str = "global",
    category: str = "preference",
    key: str = "response.language",
) -> dict:
    return {
        "scope_type": scope,
        "category": category,
        "key": key,
        "content": content,
    }


def test_approved_chinese_preference_is_typed_without_promoting_english_terms():
    policy = resolve_response_language_policy(
        [_memory("用户偏好使用中文回答，技术名词可以保留英文。")],
        "Explain what a runtime checkpoint is in two sentences.",
    )

    assert policy is not None
    assert policy.default_language is ResponseLanguage.ZH
    assert policy.effective_language is ResponseLanguage.ZH
    assert policy.current_turn_override is False


def test_workspace_preference_overrides_global_and_explicit_turn_can_override_it():
    memories = [
        _memory("默认使用中文回答。"),
        _memory("Use English responses by default.", scope="workspace"),
    ]

    workspace_policy = resolve_response_language_policy(memories, "解释 checkpoint。")
    override_policy = resolve_response_language_policy(
        memories,
        "请用中文回答：解释 checkpoint。",
    )

    assert workspace_policy is not None
    assert workspace_policy.default_language is ResponseLanguage.EN
    assert workspace_policy.source_scope == "workspace"
    assert override_policy is not None
    assert override_policy.effective_language is ResponseLanguage.ZH
    assert override_policy.current_turn_override is True


def test_unallowlisted_or_ambiguous_memory_is_never_promoted():
    assert (
        resolve_response_language_policy(
            [_memory("默认使用中文回答。", key="assistant.instructions")],
            "Explain this.",
        )
        is None
    )
    assert (
        resolve_response_language_policy(
            [
                _memory("默认使用中文回答。"),
                _memory("默认使用英文回答。"),
            ],
            "Explain this.",
        )
        is None
    )


def test_quoted_language_example_is_not_treated_as_current_turn_override():
    policy = resolve_response_language_policy(
        [_memory("默认使用中文回答。")],
        "Translate the phrase 'please answer in English' into Chinese.",
    )

    assert policy is not None
    assert policy.effective_language is ResponseLanguage.ZH
    assert policy.current_turn_override is False


def test_validator_rejects_natural_language_mismatch_but_allows_structured_output():
    state = AgentState(
        user_goal="Explain the checkpoint in two sentences.",
        memory_items=[_memory("默认使用中文回答。")],
    )
    validator = ResponseLanguagePreferenceValidator()

    rejected = validator.validate(
        action=AgentAction.finish(
            "A runtime checkpoint stores resumable state. It supports recovery after a crash."
        ),
        state=state,
    )
    accepted = validator.validate(
        action=AgentAction.finish("Runtime checkpoint 保存可恢复状态，并支持崩溃后的执行恢复。"),
        state=state,
    )
    structured = validator.validate(
        action=AgentAction.finish('{"status":"ok"}'),
        state=state,
    )

    assert rejected.accepted is False
    assert rejected.reason_code == "RESPONSE_LANGUAGE_MISMATCH"
    assert accepted.accepted is True
    assert structured.accepted is True
