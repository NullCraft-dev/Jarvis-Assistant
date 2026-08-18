import json
from uuid import uuid4

import pytest

from jarvis_worker.agent.core.conversation_constraints import (
    is_deictic_document_reference_goal,
    is_prior_answer_transform_goal,
)
from jarvis_worker.agent.intents import (
    IntentDocument,
    IntentExtraction,
    IntentRuntimeContext,
    LlmIntentExtractor,
    RuleBasedIntentExtractor,
)
from jarvis_worker.agent.intents.contracts import IntentEffects, IntentWorkspace, RetrievalIntent
from jarvis_worker.agent.intents.rules import (
    build_safe_intent_fallback,
    build_safe_workspace_effect_fallback,
    build_safe_workspace_read_fallback,
    is_explicit_scoped_directory_delete_goal,
)
from jarvis_worker.agent.models.errors import ModelProviderError


def _extract(goal: str, *, rag_available: bool = True):
    tools = frozenset({"rag.search"}) if rag_available else frozenset()
    return RuleBasedIntentExtractor().extract(goal, available_tool_names=tools)


def test_explicit_document_dependency_requires_rag():
    result = _extract("这份 PDF 中是如何解释 QLoRA 显存优化的？")

    assert result.primary_intent == "document_question"
    assert result.retrieval.mode == "required"
    assert result.retrieval.document_refs == ("这份 PDF",)


def test_implicit_professional_question_retrieves_without_database_wording():
    result = _extract("QLoRA 为什么能够降低训练显存？")

    assert result.primary_intent == "knowledge_question"
    assert result.retrieval.mode == "retrieve"
    assert result.retrieval.query == "QLoRA 为什么能够降低训练显存？"


def test_rewrite_and_greeting_skip_rag():
    assert _extract("请润色下面这段文字").retrieval.mode == "skip"
    assert _extract("你好！").retrieval.mode == "skip"


def test_prior_answer_transform_skips_rag_but_explicit_verification_does_not():
    transform = _extract("把刚才的比较结果压缩成四行表格，不要新增内容。")
    verification = _extract("重新检索并核对上一条回答的引用是否支持结论。")

    assert transform.primary_intent == "conversation"
    assert transform.retrieval.mode == "skip"
    assert verification.retrieval.mode != "skip"


def test_prior_answer_transform_does_not_swallow_explicit_external_effect():
    assert not is_prior_answer_transform_goal("把刚才的比较结果压缩成四行表格，然后保存到知识库。")


def test_deictic_document_reference_is_narrowly_recognized():
    assert is_deictic_document_reference_goal("总结刚才那份手册。")
    assert is_deictic_document_reference_goal("这篇论文的结论是什么？")
    assert is_deictic_document_reference_goal("这份 NIST 文档的核心要求是什么？")
    assert not is_deictic_document_reference_goal("总结刚才的回答。")


def test_rule_citation_verification_round_trips_as_unresolved_document_question():
    result = _extract("重新检索并核对上一条回答的引用是否支持结论。")

    restored = IntentExtraction.from_state_dict(result.to_state_dict())

    assert restored.primary_intent == "document_question"
    assert restored.retrieval.document_scope == "unresolved"


def test_explicit_opt_out_wins_over_knowledge_question():
    result = _extract("为什么 QLoRA 节省显存？不要查询知识库。")

    assert result.retrieval.mode == "skip"


@pytest.mark.parametrize(
    "goal",
    [
        "请下载这篇论文，不要提交 RAG。",
        "处理这篇论文，但不要写入向量库。",
        "总结论文，不需要向量化 RAG。",
    ],
)
def test_explicit_rag_mutation_opt_out_wins_over_paper_keyword(goal):
    assert _extract(goal).retrieval.mode == "skip"


def test_explicit_no_tool_directive_wins_over_knowledge_question():
    result = _extract("请说明为什么上下文管理很重要，不要调用任何工具。")

    assert result.retrieval.mode == "skip"
    assert result.retrieval.reason == "用户明确要求不调用工具"


def test_rag_ingestion_goal_does_not_force_rag_search():
    result = _extract("研究这些论文，把可下载的原文加入 RAG 并建立向量索引")

    assert result.primary_intent == "task"
    assert result.retrieval.mode == "skip"
    assert "摄取" in result.retrieval.reason


@pytest.mark.parametrize(
    "goal",
    [
        "下载成功后把 artifact_id 提交 RAG",
        "把这个 PDF 送入向量库，返回 pending 时不要说 ready",
    ],
)
def test_rag_submission_wording_is_ingestion_not_search(goal):
    result = _extract(goal)

    assert result.retrieval.mode == "skip"
    assert "摄取" in result.retrieval.reason


def test_missing_rag_capability_fails_closed_to_skip():
    result = _extract("QLoRA 为什么能够降低训练显存？", rag_available=False)

    assert result.retrieval.mode == "skip"
    assert result.retrieval.confidence == 1.0


def test_safe_fallback_recovers_only_read_only_workspace_evidence_goal():
    result = build_safe_workspace_read_fallback(
        "请阅读工作区中与采购申请相关的材料，核对流程并给出文件依据。",
        available_tool_names=frozenset(
            {"workspace.read_file", "workspace.read_files", "workspace.delete_path"}
        ),
    )

    assert result is not None
    assert result.source == "rule"
    assert result.retrieval.mode == "skip"
    assert result.workspace.evidence == "required"
    assert result.workspace.action == "read"
    assert result.workspace.ambiguity == "clear"


def test_safe_fallback_keeps_negated_file_creation_out_of_listing_projection():
    result = build_safe_workspace_read_fallback(
        "不要创建任何文件，只告诉我工作区根目录下有哪些一级目录。",
        available_tool_names=frozenset({"workspace.list_files"}),
    )

    assert result is not None
    assert result.workspace.evidence == "metadata"
    assert result.workspace.action == "read"
    assert result.workspace.listing_entry_types == ("dir",)


def test_safe_read_fallback_accepts_explicit_bare_directory_scope():
    result = build_safe_workspace_read_fallback(
        "列出 incoming 下的文件，按类型归组，并指出可能重复的文件；不要修改任何内容。",
        available_tool_names=frozenset(
            {"workspace.list_files", "workspace.read_file", "workspace.read_files"}
        ),
    )

    assert result is not None
    assert result.workspace.action == "read"
    assert result.workspace.evidence == "required"


@pytest.mark.parametrize("goal", ["读取 ../.env", "列出 /etc 目录", "读取 notes/*.md"])
def test_safe_read_fallback_rejects_unsafe_scopes(goal):
    assert (
        build_safe_workspace_read_fallback(
            goal,
            available_tool_names=frozenset(
                {"workspace.list_files", "workspace.read_file", "workspace.read_files"}
            ),
        )
        is None
    )


@pytest.mark.parametrize(
    ("goal", "tool_name", "action"),
    [
        ("在工作区中创建 summary.md。", "workspace.create_file", "write"),
        (
            "把 incoming/report.md 移动到 archive/report.md。",
            "workspace.move_path",
            "write",
        ),
        ("删除工作区中的 notes/old.md。", "workspace.delete_path", "destructive"),
    ],
)
def test_safe_effect_fallback_recovers_only_explicit_scoped_actions(goal, tool_name, action):
    result = build_safe_workspace_effect_fallback(
        goal,
        available_tool_names=frozenset({tool_name}),
    )

    assert result is not None
    assert result.source == "rule"
    assert result.retrieval.mode == "skip"
    assert result.workspace.evidence == "skip"
    assert result.workspace.action == action
    assert result.workspace.ambiguity == "clear"


def test_safe_effect_fallback_treats_one_named_directory_delete_as_clear_l4_scope():
    result = build_safe_workspace_effect_fallback(
        "删除 notes 目录和里面的所有内容。",
        available_tool_names=frozenset({"workspace.delete_path"}),
    )

    assert result is not None
    assert result.workspace.evidence == "skip"
    assert result.workspace.action == "destructive"
    assert result.workspace.ambiguity == "clear"


@pytest.mark.parametrize(
    "goal",
    [
        "删除 notes 目录和里面的所有内容。",
        "请删除 workspace 中 archive/old 目录及其中的全部文件。",
    ],
)
def test_named_directory_delete_scope_detector_accepts_only_complete_directory_targets(goal):
    assert is_explicit_scoped_directory_delete_goal(goal) is True


@pytest.mark.parametrize(
    "goal",
    [
        "删除 notes 目录中的所有重复文件。",
        "删除这些目录和里面的所有内容。",
        "删除 ../notes 目录和里面的所有内容。",
        "删除 notes/* 目录和里面的所有内容。",
    ],
)
def test_named_directory_delete_scope_detector_keeps_candidates_and_unsafe_paths_ambiguous(goal):
    assert is_explicit_scoped_directory_delete_goal(goal) is False


def test_safe_move_fallback_resolves_unique_recent_created_directory_from_history():
    result = build_safe_workspace_effect_fallback(
        "把 incoming/meeting-notes.md 移动到刚建的目录，其他文件不动。",
        available_tool_names=frozenset({"workspace.move_path"}),
        history_messages=(
            {
                "role": "user",
                "content": "创建 archive/2026-Q3 目录，但先不要移动文件。",
            },
            {
                "role": "assistant",
                "content": "已创建目录 archive/2026-Q3。文件暂未移动。",
            },
        ),
    )

    assert result is not None
    assert result.workspace.action == "write"
    assert result.workspace.ambiguity == "clear"
    assert "archive/2026-Q3" in result.workspace.reason


def test_safe_move_fallback_accepts_runtime_directory_success_receipt():
    result = build_safe_workspace_effect_fallback(
        "把 incoming/meeting-notes.md 移动到刚建的目录，其他文件不动。",
        available_tool_names=frozenset({"workspace.move_path"}),
        history_messages=(
            {
                "role": "user",
                "content": "列出 incoming 下的文件，按类型归组，并指出可能重复的文件；不要修改任何内容。",
            },
            {
                "role": "assistant",
                "content": "`incoming/` 下共 6 个文件，未对任何内容做修改。",
            },
            {
                "role": "user",
                "content": "创建 archive/2026-Q3 目录，用来整理刚才的文件，但先不要移动文件。",
            },
            {
                "role": "assistant",
                "content": "已在 workspace 下创建目录 `archive/2026-Q3`，可用于后续整理文件。未移动任何文件。",
            },
        ),
    )

    assert result is not None
    assert result.workspace.action == "write"
    assert result.workspace.ambiguity == "clear"
    assert "archive/2026-Q3" in result.workspace.reason


def test_safe_move_fallback_does_not_trust_ungrounded_assistant_directory_claim():
    result = build_safe_workspace_effect_fallback(
        "把 incoming/meeting-notes.md 移动到刚建的目录，其他文件不动。",
        available_tool_names=frozenset({"workspace.move_path"}),
        history_messages=(
            {"role": "user", "content": "帮我整理一下文件。"},
            {"role": "assistant", "content": "已创建目录 archive/hallucinated。"},
        ),
    )

    assert result is not None
    assert result.workspace.action == "write"
    assert result.workspace.ambiguity == "clarification_required"


def test_safe_move_fallback_does_not_treat_failed_directory_receipt_as_success():
    result = build_safe_workspace_effect_fallback(
        "把 incoming/meeting-notes.md 移动到刚建的目录，其他文件不动。",
        available_tool_names=frozenset({"workspace.move_path"}),
        history_messages=(
            {
                "role": "user",
                "content": "创建 archive/2026-Q3 目录，但先不要移动文件。",
            },
            {
                "role": "assistant",
                "content": "已在 workspace 下确认无法创建目录 `archive/2026-Q3`。",
            },
        ),
    )

    assert result is not None
    assert result.workspace.action == "write"
    assert result.workspace.ambiguity == "clarification_required"


@pytest.mark.parametrize(
    ("goal", "tool_name", "action"),
    [
        ("删除工作区中的所有重复文件。", "workspace.delete_path", "destructive"),
        ("把工作区中的报告移动到 archive。", "workspace.move_path", "write"),
    ],
)
def test_safe_effect_fallback_turns_ambiguous_scope_into_clarification(goal, tool_name, action):
    result = build_safe_workspace_effect_fallback(
        goal,
        available_tool_names=frozenset({tool_name}),
    )

    assert result is not None
    assert result.workspace.action == action
    assert result.workspace.ambiguity == "clarification_required"


@pytest.mark.parametrize(
    ("goal", "tools"),
    [
        ("把 ../secret.md 移动到 archive/secret.md。", {"workspace.move_path"}),
        ("创建 notes/new.md，但不要调用任何工具。", {"workspace.create_file"}),
        ("创建 notes/new.md。", {"workspace.read_file"}),
        ("比较两份公开政策，但不要访问工作区。", {"workspace.create_file"}),
    ],
)
def test_safe_effect_fallback_keeps_unsafe_or_unsupported_goals_failed_closed(goal, tools):
    assert (
        build_safe_workspace_effect_fallback(
            goal,
            available_tool_names=frozenset(tools),
        )
        is None
    )


def test_safe_intent_fallback_turns_unclassified_goal_into_no_capability_clarification():
    result = build_safe_intent_fallback(
        "帮我整理一下这个。",
        available_tool_names=frozenset({"workspace.list_files", "rag.search"}),
    )

    assert result is not None
    assert result.source == "rule"
    assert result.primary_intent == "unknown"
    assert result.retrieval.mode == "skip"
    assert result.effects.knowledge_write == "skip"
    assert result.effects.rag_ingestion == "skip"
    assert result.workspace.action == "none"
    assert result.workspace.evidence == "skip"


def test_safe_intent_fallback_keeps_empty_goal_failed_closed():
    assert (
        build_safe_intent_fallback(
            "   ",
            available_tool_names=frozenset({"workspace.list_files"}),
        )
        is None
    )


class _StructuredModel:
    def __init__(self, raw: str):
        self.raw = raw
        self.messages = []

    def complete_structured(self, messages, parser):
        self.messages = messages
        return parser(self.raw)


def _intent_json(
    *,
    primary_intent="document_question",
    mode="required",
    scope="selected",
    keys=None,
    knowledge_write="skip",
    knowledge_provenance="skip",
    knowledge_title="",
    rag_ingestion="skip",
    workspace_evidence="skip",
    workspace_action="none",
    workspace_ambiguity="clear",
    listing_entry_types=None,
):
    return json.dumps(
        {
            "primary_intent": primary_intent,
            "retrieval": {
                "mode": mode,
                "query": "" if mode == "skip" else "总结这份资料的关键结论",
                "confidence": 0.93,
                "reason": "问题依赖指定文档",
                "document_refs": ["这份资料"] if mode == "required" else [],
                "document_scope": scope,
                "document_keys": keys or [],
            },
            "effects": {
                "knowledge_write": knowledge_write,
                "knowledge_provenance": knowledge_provenance,
                "knowledge_title": knowledge_title,
                "rag_ingestion": rag_ingestion,
            },
            "workspace": {
                "evidence": workspace_evidence,
                "action": workspace_action,
                "ambiguity": workspace_ambiguity,
                "listing_entry_types": listing_entry_types or [],
                "reason": "工作区访问语义已分类",
            },
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    ("goal", "workspace", "expected", "listing_entry_types"),
    [
        (
            "帮我看一下这个项目的鉴权实现有没有越权风险",
            ("required", "read", "clear"),
            ("required", "read", "clear"),
            (),
        ),
        (
            "不要创建任何文件，只告诉我工作区根目录下有哪些一级目录",
            ("metadata", "read", "clear"),
            ("metadata", "read", "clear"),
            ("dir",),
        ),
        (
            "列一下根目录都有哪些文件夹，不要打开任何文件正文",
            ("metadata", "read", "clear"),
            ("metadata", "read", "clear"),
            ("dir",),
        ),
        (
            "看看 README.md 是否存在以及它有多大，不要读取正文",
            ("metadata", "read", "clear"),
            ("metadata", "read", "clear"),
            (),
        ),
        (
            "在 reports 目录生成一份检查结论，但不要覆盖已有文件",
            ("skip", "write", "clear"),
            ("skip", "write", "clear"),
            (),
        ),
        (
            "把那些旧的东西都删掉",
            ("skip", "destructive", "clarification_required"),
            ("skip", "destructive", "clarification_required"),
            (),
        ),
        (
            "删除 incoming 里所有重复文件",
            ("metadata", "destructive", "clarification_required"),
            ("skip", "destructive", "clarification_required"),
            (),
        ),
        (
            "清理 reports 中可能重复的版本，保留哪一份我还没决定",
            ("required", "destructive", "clarification_required"),
            ("skip", "destructive", "clarification_required"),
            (),
        ),
        (
            "覆盖现有报告，但还没有确定具体文件",
            ("metadata", "write", "clarification_required"),
            ("skip", "write", "clarification_required"),
            (),
        ),
        (
            "解释一下如何安全删除临时目录，只给建议",
            ("skip", "none", "clear"),
            ("skip", "none", "clear"),
            (),
        ),
    ],
)
def test_llm_workspace_semantics_follow_natural_language_not_tool_keywords(
    goal, workspace, expected, listing_entry_types
):
    evidence, action, ambiguity = workspace
    model = _StructuredModel(
        _intent_json(
            primary_intent="task",
            mode="skip",
            scope="none",
            workspace_evidence=evidence,
            workspace_action=action,
            workspace_ambiguity=ambiguity,
            listing_entry_types=listing_entry_types,
        )
    )

    result = LlmIntentExtractor(model).extract(
        goal,
        available_tool_names=frozenset(
            {
                "workspace.list_files",
                "workspace.get_file_info",
                "workspace.read_file",
                "workspace.create_file",
                "workspace.delete_path",
            }
        ),
    )

    assert (
        result.workspace.evidence,
        result.workspace.action,
        result.workspace.ambiguity,
    ) == expected
    wire_prompt = "\n".join(message.content for message in model.messages)
    assert "evidence=metadata" in wire_prompt
    assert "目录名称/类型与文件正文不能互相混淆" in wire_prompt
    assert "listing_entry_types" in wire_prompt
    assert "阅读代码库/项目源码/仓库文件" in wire_prompt
    assert "clarification_required 时 evidence 必须使用 skip" in wire_prompt


def test_llm_intent_host_arbitrates_named_directory_delete_to_l4_confirmation_scope():
    model = _StructuredModel(
        _intent_json(
            primary_intent="task",
            mode="skip",
            scope="none",
            workspace_evidence="skip",
            workspace_action="destructive",
            workspace_ambiguity="clarification_required",
        )
    )

    result = LlmIntentExtractor(model).extract(
        "删除 notes 目录和里面的所有内容。",
        available_tool_names=frozenset({"workspace.delete_path"}),
    )

    assert result.workspace.evidence == "skip"
    assert result.workspace.action == "destructive"
    assert result.workspace.ambiguity == "clear"
    assert "L4" in result.workspace.reason


def test_llm_intent_does_not_arbitrate_partial_candidates_inside_directory():
    model = _StructuredModel(
        _intent_json(
            primary_intent="task",
            mode="skip",
            scope="none",
            workspace_evidence="skip",
            workspace_action="destructive",
            workspace_ambiguity="clarification_required",
        )
    )

    result = LlmIntentExtractor(model).extract(
        "删除 notes 目录中的所有重复文件。",
        available_tool_names=frozenset({"workspace.delete_path"}),
    )

    assert result.workspace.ambiguity == "clarification_required"


def test_llm_intent_preserves_strict_workspace_listing_types():
    model = _StructuredModel(
        _intent_json(
            primary_intent="task",
            mode="skip",
            scope="none",
            workspace_evidence="metadata",
            workspace_action="read",
            listing_entry_types=["dir"],
        )
    )

    result = LlmIntentExtractor(model).extract(
        "不要创建任何文件，只告诉我工作区根目录下有哪些一级目录",
        available_tool_names=frozenset({"workspace.list_files"}),
    )

    assert result.workspace.listing_entry_types == ("dir",)


def test_llm_intent_rejects_listing_projection_for_non_metadata_action():
    model = _StructuredModel(
        _intent_json(
            primary_intent="task",
            mode="skip",
            scope="none",
            workspace_evidence="skip",
            workspace_action="write",
            listing_entry_types=["file"],
        )
    )

    with pytest.raises(ModelProviderError) as error:
        LlmIntentExtractor(model).extract(
            "创建一个文件",
            available_tool_names=frozenset({"workspace.create_file"}),
        )

    assert error.value.output_failure_kind == "schema_violation"


def test_llm_intent_rejects_listing_types_that_conflict_with_explicit_goal():
    model = _StructuredModel(
        _intent_json(
            primary_intent="task",
            mode="skip",
            scope="none",
            workspace_evidence="metadata",
            workspace_action="read",
            listing_entry_types=["file", "dir"],
        )
    )

    with pytest.raises(ModelProviderError) as error:
        LlmIntentExtractor(model).extract(
            "不要创建任何文件，只告诉我工作区根目录下有哪些一级目录",
            available_tool_names=frozenset({"workspace.list_files"}),
        )

    assert error.value.output_failure_kind == "schema_violation"


def test_intent_state_restores_legacy_v5_without_listing_projection():
    legacy = IntentExtraction(
        primary_intent="task",
        retrieval=RetrievalIntent(
            mode="skip",
            query="",
            confidence=1.0,
            reason="旧检查点",
        ),
        effects=IntentEffects(),
        workspace=IntentWorkspace(
            evidence="metadata",
            action="read",
            ambiguity="clear",
            reason="旧目录读取",
        ),
    ).to_state_dict()
    legacy["policy_version"] = "intent-llm-v5"
    legacy["workspace"].pop("listing_entry_types")

    restored = IntentExtraction.from_state_dict(legacy)

    assert restored.workspace.listing_entry_types == ()
    assert restored.policy_version == "intent-llm-v7"


def test_intent_state_restores_legacy_v6_with_listing_projection():
    legacy = IntentExtraction(
        primary_intent="task",
        retrieval=RetrievalIntent(
            mode="skip",
            query="",
            confidence=1.0,
            reason="旧检查点",
        ),
        workspace=IntentWorkspace(
            evidence="metadata",
            action="read",
            ambiguity="clear",
            listing_entry_types=("dir",),
            reason="旧目录读取",
        ),
    ).to_state_dict()
    legacy["policy_version"] = "intent-llm-v6"

    restored = IntentExtraction.from_state_dict(legacy)

    assert restored.workspace.listing_entry_types == ("dir",)
    assert restored.policy_version == "intent-llm-v7"


@pytest.mark.parametrize(
    ("workspace_evidence", "workspace_action", "workspace_ambiguity"),
    [
        ("metadata", "destructive", "clear"),
        ("required", "write", "clear"),
        ("metadata", "read", "clarification_required"),
    ],
)
def test_llm_intent_keeps_invalid_non_ambiguous_workspace_combinations_fail_closed(
    workspace_evidence, workspace_action, workspace_ambiguity
):
    model = _StructuredModel(
        _intent_json(
            primary_intent="task",
            mode="skip",
            scope="none",
            workspace_evidence=workspace_evidence,
            workspace_action=workspace_action,
            workspace_ambiguity=workspace_ambiguity,
        )
    )

    with pytest.raises(ModelProviderError) as error:
        LlmIntentExtractor(model).extract(
            "处理工作区内容",
            available_tool_names=frozenset({"workspace.read_file", "workspace.delete_path"}),
        )

    assert error.value.output_failure_kind == "schema_violation"


def test_llm_intent_maps_anonymous_document_key_to_trusted_runtime_id():
    document_id = str(uuid4())
    context = IntentRuntimeContext(
        (
            IntentDocument(
                key="doc_1",
                document_id=document_id,
                title="QLoRA 论文",
                created_at="2026-07-30T00:00:00+00:00",
            ),
        )
    )
    model = _StructuredModel(_intent_json(keys=["doc_1"]))

    result = LlmIntentExtractor(model).extract(
        "总结刚才那份 QLoRA 论文",
        available_tool_names=frozenset({"rag.search"}),
        runtime_context=context,
    )

    assert result.source == "llm"
    assert result.retrieval.document_scope == "selected"
    assert result.retrieval.resolved_document_ids == (document_id,)
    wire_prompt = "\n".join(message.content for message in model.messages)
    assert "doc_1" in wire_prompt
    assert "QLoRA 论文" in wire_prompt
    assert document_id not in wire_prompt


def test_llm_intent_new_conversation_deictic_document_reference_requires_clarification():
    document_id, other_id = str(uuid4()), str(uuid4())
    context = IntentRuntimeContext(
        (
            IntentDocument(
                key="doc_1",
                document_id=document_id,
                title="nasa-systems-engineering-handbook-rev2.pdf",
                created_at="2026-08-01T00:00:00+00:00",
            ),
            IntentDocument(
                key="doc_2",
                document_id=other_id,
                title="nist-ai-rmf-1-0.pdf",
                created_at="2026-08-02T00:00:00+00:00",
            ),
        )
    )
    model = _StructuredModel(
        _intent_json(
            primary_intent="task",
            mode="skip",
            scope="none",
            workspace_evidence="required",
            workspace_action="read",
        )
    )

    result = LlmIntentExtractor(model).extract(
        "总结刚才那份手册。",
        available_tool_names=frozenset({"rag.search", "workspace.search_text"}),
        runtime_context=context,
        history_messages=(),
    )

    assert result.primary_intent == "document_question"
    assert result.retrieval.document_scope == "unresolved"
    assert result.retrieval.resolved_document_ids == ()
    assert result.workspace.action == "none"
    IntentExtraction.from_state_dict(result.to_state_dict())


def test_llm_intent_promotes_unique_explicit_document_identity_from_unresolved():
    nist_id, nasa_id = str(uuid4()), str(uuid4())
    context = IntentRuntimeContext(
        (
            IntentDocument(
                key="doc_1",
                document_id=nist_id,
                title="nist-ai-rmf-1-0.pdf",
                created_at="2026-08-01T00:00:00+00:00",
            ),
            IntentDocument(
                key="doc_2",
                document_id=nasa_id,
                title="nasa-systems-engineering-handbook-rev2.pdf",
                created_at="2026-08-02T00:00:00+00:00",
            ),
        )
    )
    model = _StructuredModel(
        _intent_json(
            primary_intent="document_question",
            mode="required",
            scope="unresolved",
        )
    )

    result = LlmIntentExtractor(model).extract(
        "这份 NIST 文档对 GOVERN 函数提出了哪些核心要求？",
        available_tool_names=frozenset({"rag.search"}),
        runtime_context=context,
    )

    assert result.primary_intent == "document_question"
    assert result.retrieval.document_scope == "selected"
    assert result.retrieval.resolved_document_ids == (nist_id,)
    assert result.retrieval.document_refs == ("nist-ai-rmf-1-0.pdf",)
    IntentExtraction.from_state_dict(result.to_state_dict())


def test_llm_intent_restores_unique_explicit_deictic_document_when_model_skips_rag():
    nist_id, nasa_id = str(uuid4()), str(uuid4())
    context = IntentRuntimeContext(
        (
            IntentDocument(
                key="doc_1",
                document_id=nist_id,
                title="nist-ai-rmf-1-0.pdf",
                created_at="2026-08-01T00:00:00+00:00",
            ),
            IntentDocument(
                key="doc_2",
                document_id=nasa_id,
                title="nasa-systems-engineering-handbook-rev2.pdf",
                created_at="2026-08-02T00:00:00+00:00",
            ),
        )
    )
    model = _StructuredModel(
        _intent_json(
            primary_intent="task",
            mode="skip",
            scope="none",
            workspace_evidence="required",
            workspace_action="read",
        )
    )

    result = LlmIntentExtractor(model).extract(
        "这份 NIST 文档对 GOVERN 函数提出了哪些核心要求？只用这份文档回答。",
        available_tool_names=frozenset({"rag.search", "workspace.search_text"}),
        runtime_context=context,
    )

    assert result.primary_intent == "document_question"
    assert result.retrieval.mode == "required"
    assert result.retrieval.document_scope == "selected"
    assert result.retrieval.resolved_document_ids == (nist_id,)
    assert result.workspace.action == "none"
    IntentExtraction.from_state_dict(result.to_state_dict())


def test_llm_citation_verification_binds_requested_ordinal_to_trusted_history_document():
    first_id, second_id = str(uuid4()), str(uuid4())
    context = IntentRuntimeContext(
        (
            IntentDocument("doc_1", first_id, "first.pdf", "2026-08-01T00:00:00+00:00"),
            IntentDocument("doc_2", second_id, "second.pdf", "2026-08-02T00:00:00+00:00"),
        )
    )
    model = _StructuredModel(
        _intent_json(
            primary_intent="research_task",
            mode="required",
            scope="all",
            keys=[],
        )
    )

    result = LlmIntentExtractor(model).extract(
        "你上一条回答的第二个引用真的支持前一句吗？重新核对原文。",
        available_tool_names=frozenset({"rag.search"}),
        runtime_context=context,
        history_messages=(
            {
                "role": "assistant",
                "content": "引用：\n- [1] first.pdf · p.1 (`chunk:one`)\n- [2] second.pdf · p.7 (`chunk:two`)",
            },
        ),
    )

    assert result.primary_intent == "document_question"
    assert result.retrieval.document_scope == "selected"
    assert result.retrieval.resolved_document_ids == (second_id,)
    IntentExtraction.from_state_dict(result.to_state_dict())


def test_llm_intent_prefers_direct_workspace_evidence_for_follow_up_entity_reference():
    document_id = str(uuid4())
    context = IntentRuntimeContext(
        (
            IntentDocument(
                key="doc_1",
                document_id=document_id,
                title="采购审批政策",
                created_at="2026-08-01T00:00:00+00:00",
            ),
        )
    )
    model = _StructuredModel(
        _intent_json(
            primary_intent="document_question",
            mode="required",
            scope="selected",
            keys=["doc_1"],
            workspace_evidence="required",
            workspace_action="read",
        )
    )

    result = LlmIntentExtractor(model).extract(
        "核对刚才采购申请适用的审批门槛，并检查操作流程和实际记录是否一致。",
        available_tool_names=frozenset(
            {"rag.search", "workspace.read_file", "workspace.read_files"}
        ),
        runtime_context=context,
    )

    assert result.primary_intent == "task"
    assert result.retrieval.mode == "skip"
    assert result.retrieval.document_scope == "none"
    assert result.retrieval.resolved_document_ids == ()
    assert result.workspace.evidence == "required"


def test_llm_intent_keeps_explicit_rag_scope_alongside_workspace_evidence():
    document_id = str(uuid4())
    context = IntentRuntimeContext(
        (
            IntentDocument(
                key="doc_1",
                document_id=document_id,
                title="采购审批政策",
                created_at="2026-08-01T00:00:00+00:00",
            ),
        )
    )
    model = _StructuredModel(
        _intent_json(
            mode="required",
            scope="selected",
            keys=["doc_1"],
            workspace_evidence="required",
            workspace_action="read",
        )
    )

    result = LlmIntentExtractor(model).extract(
        "对比知识库中已索引的采购政策与本地工作区记录。",
        available_tool_names=frozenset(
            {"rag.search", "workspace.read_file", "workspace.read_files"}
        ),
        runtime_context=context,
    )

    assert result.retrieval.mode == "required"
    assert result.retrieval.document_scope == "selected"
    assert result.retrieval.resolved_document_ids == (document_id,)
    assert result.workspace.evidence == "required"


def test_llm_intent_maps_multiple_document_keys_in_runtime_catalog_order():
    document_ids = (str(uuid4()), str(uuid4()))
    context = IntentRuntimeContext(
        tuple(
            IntentDocument(
                key=f"doc_{index}",
                document_id=document_id,
                title=title,
                created_at=f"2026-07-{31 - index:02d}T00:00:00+00:00",
            )
            for index, (document_id, title) in enumerate(
                zip(document_ids, ("Transformer 论文", "MobileNet 论文"), strict=True),
                1,
            )
        )
    )
    model = _StructuredModel(_intent_json(keys=["doc_1", "doc_2"]))

    result = LlmIntentExtractor(model).extract(
        "比较 Transformer 论文和 MobileNet 论文并保存报告",
        available_tool_names=frozenset({"rag.search", "knowledge.create_document"}),
        runtime_context=context,
    )

    assert result.retrieval.document_scope == "selected"
    assert result.retrieval.resolved_document_ids == document_ids
    wire_prompt = "\n".join(message.content for message in model.messages)
    assert all(document_id not in wire_prompt for document_id in document_ids)


def test_llm_intent_rejects_semantic_document_mismatch_against_trusted_excerpt():
    document_ids = tuple(str(uuid4()) for _ in range(3))
    context = IntentRuntimeContext(
        (
            IntentDocument(
                key="doc_1",
                document_id=document_ids[0],
                title="arXiv 1512.03385.pdf",
                created_at="2026-07-30T00:00:00+00:00",
                identity_excerpt="Deep Residual Learning for Image Recognition ResNet",
            ),
            IntentDocument(
                key="doc_2",
                document_id=document_ids[1],
                title="arXiv 1602.07360.pdf",
                created_at="2026-07-29T00:00:00+00:00",
                identity_excerpt="SqueezeNet: AlexNet-level accuracy with 50x fewer parameters",
            ),
            IntentDocument(
                key="doc_3",
                document_id=document_ids[2],
                title="arXiv 1502.03167.pdf",
                created_at="2026-07-28T00:00:00+00:00",
                identity_excerpt="Batch Normalization: Accelerating Deep Network Training",
            ),
        )
    )
    model = _StructuredModel(_intent_json(keys=["doc_1", "doc_3"]))

    with pytest.raises(ModelProviderError) as error:
        LlmIntentExtractor(model).extract(
            "帮我对比知识库里 ResNet 和 SqueezeNet 那两篇论文",
            available_tool_names=frozenset({"rag.search"}),
            runtime_context=context,
        )

    assert error.value.output_failure_kind == "schema_violation"


def test_llm_intent_does_not_treat_query_vocabulary_as_document_identity():
    transformer_id, recurrent_id = str(uuid4()), str(uuid4())
    context = IntentRuntimeContext(
        (
            IntentDocument(
                key="doc_1",
                document_id=transformer_id,
                title="arXiv 1706.03762.pdf",
                created_at="2026-07-30T00:00:00+00:00",
                identity_excerpt="Attention Is All You Need",
            ),
            IntentDocument(
                key="doc_2",
                document_id=recurrent_id,
                title="Another document.pdf",
                created_at="2026-07-29T00:00:00+00:00",
                identity_excerpt="A recurrent layer tutorial",
            ),
        )
    )

    result = LlmIntentExtractor(_StructuredModel(_intent_json(keys=["doc_1"]))).extract(
        "《Attention Is All You Need》（arXiv 1706.03762）的复杂度表里，"
        "self-attention 和 recurrent layer 分别是什么？",
        available_tool_names=frozenset({"rag.search"}),
        runtime_context=context,
    )

    assert result.retrieval.resolved_document_ids == (transformer_id,)


def test_llm_intent_downgrades_duplicate_explicit_identity_to_unresolved():
    context = IntentRuntimeContext(
        tuple(
            IntentDocument(
                key=f"doc_{index}",
                document_id=str(uuid4()),
                title="arXiv 1704.04861.pdf",
                created_at=f"2026-07-{30 - index:02d}T00:00:00+00:00",
                identity_excerpt="MobileNets: Efficient Convolutional Neural Networks",
            )
            for index in (1, 2)
        )
    )

    result = LlmIntentExtractor(_StructuredModel(_intent_json(keys=["doc_1"]))).extract(
        "请只根据 arXiv 1704.04861.pdf 的其中一份文档总结 MobileNet。",
        available_tool_names=frozenset({"rag.search"}),
        runtime_context=context,
    )

    assert result.retrieval.document_scope == "unresolved"
    assert result.retrieval.resolved_document_ids == ()


def test_llm_intent_keeps_knowledge_and_rag_effects_independent():
    model = _StructuredModel(
        _intent_json(
            primary_intent="knowledge_write",
            mode="skip",
            scope="none",
            knowledge_write="required",
            rag_ingestion="skip",
        )
    )

    result = LlmIntentExtractor(model).extract(
        "把总结保存到个人知识库，但不要加入 RAG",
        available_tool_names=frozenset({"knowledge.create_document", "rag.ingest_artifact"}),
    )

    assert result.effects.knowledge_write == "required"
    assert result.effects.rag_ingestion == "skip"
    assert result.retrieval.query == ""
    assert IntentExtraction.from_state_dict(result.to_state_dict()) == result


@pytest.mark.parametrize(
    ("primary_intent", "workspace_action", "workspace_ambiguity"),
    [
        ("unknown", "none", "clear"),
        ("task", "write", "clarification_required"),
    ],
)
def test_llm_intent_recovers_explicit_file_target_from_conservative_candidate(
    primary_intent, workspace_action, workspace_ambiguity
):
    model = _StructuredModel(
        _intent_json(
            primary_intent=primary_intent,
            mode="skip",
            scope="none",
            workspace_action=workspace_action,
            workspace_ambiguity=workspace_ambiguity,
        )
    )

    result = LlmIntentExtractor(model).extract(
        "创建 redis-loss.txt，在 pending 权限时重建专用 Redis，再恢复服务并批准。",
        available_tool_names=frozenset({"workspace.create_file"}),
    )

    assert result.primary_intent == "task"
    assert result.workspace.evidence == "skip"
    assert result.workspace.action == "write"
    assert result.workspace.ambiguity == "clear"


def test_llm_intent_allows_empty_query_for_rag_ingestion_without_search():
    model = _StructuredModel(
        _intent_json(
            primary_intent="rag_ingestion",
            mode="skip",
            scope="none",
            knowledge_write="skip",
            rag_ingestion="required",
        )
    )

    result = LlmIntentExtractor(model).extract(
        "把这个可下载的 PDF 加入资料库",
        available_tool_names=frozenset({"rag.ingest_artifact"}),
    )

    assert result.retrieval.mode == "skip"
    assert result.retrieval.query == ""
    assert result.effects.rag_ingestion == "required"


def test_llm_intent_still_requires_query_when_retrieval_is_enabled():
    candidate = json.loads(_intent_json(keys=["doc_1"]))
    candidate["retrieval"]["query"] = ""

    with pytest.raises(ModelProviderError) as error:
        LlmIntentExtractor(_StructuredModel(json.dumps(candidate))).extract(
            "总结这份资料",
            available_tool_names=frozenset({"rag.search"}),
        )

    assert error.value.output_failure_kind == "empty_field"


def test_llm_intent_rejects_unknown_document_key():
    model = _StructuredModel(_intent_json(keys=["doc_99"]))

    with pytest.raises(ModelProviderError) as error:
        LlmIntentExtractor(model).extract(
            "总结这份资料",
            available_tool_names=frozenset({"rag.search"}),
            runtime_context=IntentRuntimeContext(),
        )

    assert error.value.code == "MODEL_OUTPUT_INVALID"
    assert error.value.output_failure_kind == "schema_violation"


def test_llm_intent_rejects_extra_or_duplicate_fields():
    with_extra = json.loads(_intent_json(keys=["doc_1"]))
    with_extra["download"] = True
    model = _StructuredModel(json.dumps(with_extra))
    with pytest.raises(ModelProviderError) as extra_error:
        LlmIntentExtractor(model).extract(
            "总结资料",
            available_tool_names=frozenset({"rag.search"}),
        )
    assert extra_error.value.output_failure_kind == "unexpected_field"

    duplicate = _intent_json(primary_intent="task", mode="skip", scope="none").replace(
        '"primary_intent": "task"', '"primary_intent": "task", "primary_intent": "task"'
    )
    with pytest.raises(ModelProviderError) as duplicate_error:
        LlmIntentExtractor(_StructuredModel(duplicate)).extract(
            "处理资料",
            available_tool_names=frozenset(),
        )
    assert duplicate_error.value.output_failure_kind == "duplicate_field"


def test_llm_intent_allows_unresolved_specific_reference_without_global_fallback():
    model = _StructuredModel(_intent_json(scope="unresolved"))

    result = LlmIntentExtractor(model).extract(
        "总结刚才那篇论文",
        available_tool_names=frozenset({"rag.search"}),
        runtime_context=IntentRuntimeContext(),
    )

    assert result.retrieval.document_scope == "unresolved"
    assert result.retrieval.resolved_document_ids == ()
