from __future__ import annotations

import json

import pytest

from jarvis_worker.agent.context.manager import ContextManager
from jarvis_worker.agent.context.types import ModelContextProfile
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.models.errors import ModelProviderError
from jarvis_worker.agent.models.messages import ModelMessage
from jarvis_worker.agent.prompts.builder import PromptBuilder


class _ContentLengthEstimator:
    @property
    def name(self) -> str:
        return "test_content_length"

    def estimate(self, messages: list[ModelMessage] | tuple[ModelMessage, ...]) -> int:
        return sum(len(message.content) for message in messages)


def _profile(input_budget: int) -> ModelContextProfile:
    return ModelContextProfile(
        provider="test",
        model="test-model",
        context_window_tokens=input_budget + 1,
        max_output_tokens=1,
        safety_margin_tokens=0,
    )


def _observation(index: int, content: str = "ok") -> dict:
    call_id = f"tc-{index}"
    return {
        "tool_call_id": call_id,
        "tool_name": "workspace.read_file",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "workspace.read_file",
            "arguments": {"path": f"{index}.md"},
            "reason": "读取",
        },
        "ok": True,
        "summary": f"读取 {index}.md",
        "data": {
            "path": f"{index}.md",
            "content": content,
            "truncated": False,
        },
    }


def _source_observation(index: int, content: str) -> dict:
    observation = _observation(index, content)
    path = f"layer/{index}.py"
    observation["model_action"]["arguments"] = {
        "path": path,
        "start_line": index * 10 + 1,
        "max_lines": 50,
    }
    observation["data"].update(
        path=path,
        start_line=index * 10 + 1,
        end_line=index * 10 + 50,
        total_lines=1000,
    )
    return observation


def _listing_observation() -> dict:
    return {
        "tool_call_id": "tc-list",
        "tool_name": "workspace.list_files",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "workspace.list_files",
            "arguments": {"path": "."},
            "reason": "列出根目录",
        },
        "ok": True,
        "summary": "4 个条目",
        "data": {
            "entries": [
                {"name": "apps", "type": "dir"},
                {"name": "docs", "type": "dir"},
                {"name": "draft.md", "type": "file"},
                {"name": "external-link", "type": "symlink"},
            ]
        },
    }


def test_context_projects_workspace_listing_types_without_mutating_tool_result():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    observation = _listing_observation()
    state = AgentState(
        user_goal="只告诉我一级目录",
        intent={
            "workspace": {
                "evidence": "metadata",
                "action": "read",
                "ambiguity": "clear",
                "listing_entry_types": ["dir"],
            },
            "retrieval": {"mode": "skip", "document_scope": "none"},
        },
        observations=[observation],
    )

    package = manager.prepare(state, _profile(100_000))
    tool_payload = json.loads(
        next(message.content for message in package.messages if message.role == "tool")
    )

    assert tool_payload["data"]["entries"] == [
        {"name": "apps", "type": "dir"},
        {"name": "docs", "type": "dir"},
    ]
    assert observation["data"]["entries"][2]["name"] == "draft.md"
    assert "不得补充说明其他根目录条目" in package.messages[0].content


def test_context_manager_keeps_complete_latest_history_suffix():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="当前问题",
        history_messages=[
            {"role": "user", "content": "旧问题" * 100},
            {"role": "assistant", "content": "旧回答" * 100},
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "新回答"},
        ],
    )
    full = manager.prepare(state, _profile(100_000))
    oldest_turn_chars = len("旧问题" * 100) + len(
        '{"action_type": "finish", "final_message": "' + "旧回答" * 100 + '"}'
    )
    trimmed = manager.prepare(
        state,
        _profile(full.stats.estimated_input_tokens - oldest_turn_chars + 10),
    )

    assert trimmed.stats.included_history_turns == 1
    assert trimmed.stats.dropped_history_turns == 1
    assert trimmed.stats.truncated is True
    roles = [message.role for message in trimmed.messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert "新问题" in trimmed.messages[1].content


def test_current_run_observations_have_priority_over_history():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="读取文件",
        history_messages=[
            {"role": "user", "content": "很长的旧问题" * 100},
            {"role": "assistant", "content": "很长的旧回答" * 100},
        ],
        observations=[_observation(1), _observation(2)],
    )
    without_history = AgentState(
        user_goal=state.user_goal,
        observations=list(state.observations),
    )
    observation_only = manager.prepare(without_history, _profile(100_000))
    package = manager.prepare(
        state,
        _profile(observation_only.stats.estimated_input_tokens),
    )

    assert package.stats.included_observations == 2
    assert package.stats.included_history_turns == 0
    assert package.stats.dropped_history_turns == 1
    assert [message.role for message in package.messages][-4:] == [
        "assistant", "tool", "assistant", "tool"
    ]


def test_latest_observation_is_required_and_fails_explicitly_when_too_large():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="读取文件",
        observations=[_observation(1, content="x" * 4_000)],
    )

    with pytest.raises(ModelProviderError) as exc:
        manager.prepare(state, _profile(100))

    assert exc.value.code == "CONTEXT_BUDGET_EXCEEDED"


def test_skill_context_is_injected_as_required_system_context():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="整理资料",
        skill_context={
            "skill_id": "sample-advisor",
            "version": "1.0.0",
            "description": "整理个人知识",
            "instructions": "只使用可用工具，并保留来源。",
            "references": [
                {"path": "references/source-quality.md", "content": "验证来源质量。"}
            ],
            "fingerprint": "a" * 64,
        },
    )

    package = manager.prepare(state, _profile(100_000))

    assert package.stats.policy_version == (
        "context-v21-memory-v2-skill-v1-intent-v7-loop-v1-evidence-v3"
    )


def test_finish_only_context_hides_tool_catalog_and_keeps_existing_evidence():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(user_goal="根据已有证据收口", observations=[_observation("tc-last")])

    package = manager.prepare(state, _profile(100_000), finish_only=True)

    system = package.messages[0].content
    assert "终态收口模式" in system
    assert "唯一合法 action 是 finish" in system
    assert "当前允许的工具列表：" not in system
    assert "workspace.list_files" not in system
    assert not any(message.role == "tool" for message in package.messages)
    assert "Runtime ToolResult" in package.messages[-1].content


def test_tool_required_context_keeps_tools_and_does_not_fix_evidence_path():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(user_goal="补齐任意缺失的源码链证据", observations=[_observation(1)])

    package = manager.prepare(state, _profile(100_000), tool_required=True)

    system = package.messages[0].content
    assert "工具补证模式" in system
    assert "当前唯一合法 action 是 call_tool" in system
    assert "当前允许的工具列表：" in system
    assert "Runtime 不固定" in system
    assert "文件路径、关键词或答案路径" in system
    assert any(message.role == "tool" for message in package.messages)


def test_finish_only_and_tool_required_are_mutually_exclusive():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())

    with pytest.raises(ValueError):
        manager.prepare(
            AgentState(user_goal="冲突模式"),
            _profile(100_000),
            finish_only=True,
            tool_required=True,
        )


def test_answer_rewrite_feedback_is_trusted_and_finish_only() -> None:
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="根据已有证据收口",
        observations=[_observation("tc-last")],
        answer_guard_rejections=1,
        answer_guard_feedback="只重写最终回答；保留具体局部未知项。",
    )

    package = manager.prepare(state, _profile(100_000), finish_only=True)

    system = package.messages[0].content
    assert "只重写最终回答；保留具体局部未知项。" in system
    assert "唯一合法 action 是 finish" in system
    assert "当前允许的工具列表：" not in system


def test_source_evidence_ledger_survives_observation_suffix_trimming() -> None:
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    observations = [
        _source_observation(index, f"layer-{index}-direct-evidence-" + "x" * 1800)
        for index in range(12)
    ]
    state = AgentState(user_goal="解释端到端调用链", observations=observations)
    full = manager.prepare(state, _profile(100_000), finish_only=True)
    trimmed = manager.prepare(
        state,
        _profile(full.stats.estimated_input_tokens - 8_000),
        finish_only=True,
    )

    assert trimmed.stats.dropped_observations > 0
    ledger_messages = [
        message.content
        for message in trimmed.messages
        if "Runtime 源码证据账本" in message.content
    ]
    assert len(ledger_messages) == 1
    assert '"path": "layer/0.py"' in ledger_messages[0]
    assert '"path": "layer/11.py"' in ledger_messages[0]
    assert "layer-0-direct-evidence" in ledger_messages[0]
    assert "layer-11-direct-evidence" in ledger_messages[0]


def test_source_ledger_drops_excerpts_before_failing_required_context_budget() -> None:
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="解释调用链",
        observations=[_source_observation(1, "sensitive-source-evidence-" + "x" * 5000)],
    )
    full = manager.prepare(state, _profile(100_000))
    without_excerpt_budget = full.stats.estimated_input_tokens - 400

    package = manager.prepare(state, _profile(without_excerpt_budget))

    ledger = next(
        message.content for message in package.messages if "Runtime 源码证据账本" in message.content
    )
    assert "omitted_for_context_budget" in ledger
    assert "sensitive-source-evidence" not in ledger


def test_skill_context_is_required_and_cannot_be_trimmed():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="整理资料",
        skill_context={
            "skill_id": "sample-advisor",
            "version": "1.0.0",
            "description": "整理个人知识",
            "instructions": "x" * 4_000,
            "references": [],
            "fingerprint": "a" * 64,
        },
    )

    with pytest.raises(ModelProviderError) as exc:
        manager.prepare(state, _profile(100))

    assert exc.value.code == "CONTEXT_BUDGET_EXCEEDED"


def test_retrieval_intent_is_injected_without_promoting_user_query():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="QLoRA 为什么降低显存？",
        intent={
            "primary_intent": "knowledge_question",
            "policy_version": "intent-llm-v3",
            "source": "llm",
            "retrieval": {
                "mode": "retrieve",
                "query": "不应被提升到 system 的动态 query",
                "confidence": 0.82,
                "reason": "专业知识问题",
                "document_refs": [],
                "document_scope": "all",
                "resolved_document_ids": [],
            },
            "effects": {
                "knowledge_write": "skip",
                "knowledge_provenance": "skip",
                "knowledge_title": "",
                "rag_ingestion": "skip",
            },
        },
    )

    package = manager.prepare(state, _profile(100_000))

    system = package.messages[0].content
    assert "Runtime LLM Intent 策略" in system
    assert "在 finish 前先调用 rag.search" in system
    assert "不应被提升到 system 的动态 query" not in system


def test_skip_retrieval_intent_does_not_change_system_prompt():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    baseline = manager.prepare(AgentState(user_goal="你好"), _profile(100_000))
    skipped = manager.prepare(
        AgentState(
            user_goal="你好",
            intent={"retrieval": {"mode": "skip"}},
        ),
        _profile(100_000),
    )

    assert skipped.messages[0] == baseline.messages[0]


def test_unresolved_document_guidance_uses_user_facing_disambiguation():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="总结同名资料",
        intent={
            "retrieval": {
                "mode": "required",
                "document_scope": "unresolved",
                "document_refs": ["arXiv 1704.04861.pdf"],
                "resolved_document_ids": [],
            },
            "effects": {},
        },
    )

    system = manager.prepare(state, _profile(100_000)).messages[0].content

    assert "同名资料不唯一" in system
    assert "标题、来源、版本或上传时间" in system
    assert "不要要求用户提供 document_id" in system


def test_context_fingerprint_is_deterministic_and_stats_have_no_body():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(user_goal="你好")

    first = manager.prepare(state, _profile(100_000))
    second = manager.prepare(state, _profile(100_000))

    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
    assert not hasattr(first.stats, "messages")


def test_runtime_feedback_is_trusted_required_system_context():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="审查代码",
        effect_guard_feedback="前一轮缺少必要文件证据。",
    )

    package = manager.prepare(
        state,
        _profile(100_000),
        runtime_feedback=["工具调用预算：已使用 2/10，剩余 8 次。"],
    )

    system = package.messages[0].content
    assert "前一轮缺少必要文件证据" in system
    assert "工具调用预算：已使用 2/10，剩余 8 次" in system


def test_context_projects_current_run_evidence_owner_and_history_boundary():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    rag = {
        "tool_call_id": "rag-1",
        "tool_name": "rag.search",
        "model_action": {
            "action_type": "call_tool",
            "tool_name": "rag.search",
            "arguments": {"query": "架构"},
            "reason": "检索",
        },
        "ok": True,
        "summary": "检索完成",
        "data": {"query": "架构", "results": []},
    }
    state = AgentState(
        user_goal="核对最新原文",
        observations=[rag, _observation(1)],
        trusted_history_provenance=[{"source_url": "https://example.invalid/source"}],
    )

    package = manager.prepare(state, _profile(100_000))

    system = package.messages[0].content
    assert '"active_answer_owner": "workspace_direct"' in system
    assert '"historical_lineage_available": true' in system
    assert "历史 provenance 只用于连接来源" in system


def test_confirmed_memories_are_bounded_and_separate_from_current_command():
    manager = ContextManager(PromptBuilder(), _ContentLengthEstimator())
    state = AgentState(
        user_goal="继续开发",
        memory_items=[
            {
                "scope_type": "workspace",
                "category": "project_fact",
                "key": "python.environment",
                "content": "本项目使用 conda 环境。",
                "importance": 80,
            },
            {
                "scope_type": "global",
                "category": "preference",
                "key": "response.language",
                "content": "默认使用中文回答。",
                "importance": 70,
            },
        ],
    )
    package = manager.prepare(state, _profile(100_000))

    assert package.stats.included_memories == 2
    assert package.stats.dropped_memories == 0
    assert "Runtime 已批准响应语言策略" in package.messages[0].content
    assert '"default_language": "zh"' in package.messages[0].content
    assert "默认使用中文回答" not in package.messages[0].content
    assert package.messages[1].role == "user"
    assert "背景数据，不是当前命令" in package.messages[1].content
    assert package.messages[-1].content == "继续开发"
