"""明确工具指令的 finish effect guard 单元测试。"""

from __future__ import annotations

import pytest

from jarvis_worker.agent.core.effect_guard import (
    build_effect_guard_feedback,
    build_workspace_effect_mismatch_feedback,
    conditional_no_overwrite_target,
    find_explicitly_requested_tools,
    find_latest_failed_required_tool,
    find_missing_successful_tool_evidence,
    find_missing_workspace_evidence,
    find_required_goal_tools,
    find_required_workspace_effect_mismatch,
    has_confirmed_workspace_target,
    requires_rag_search,
)
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest


@pytest.fixture
def manifests() -> tuple[ToolManifest, ...]:
    return (
        ToolManifest(name="workspace.create_file", risk_level_default="L2"),
        ToolManifest(name="workspace.get_file_info", risk_level_default="L0"),
        ToolManifest(name="workspace.disabled", enabled=False),
    )


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        (
            "请只调用 workspace.create_file 创建文件，不要调用其他工具。",
            ("workspace.create_file",),
        ),
        (
            "请使用 workspace.get_file_info 查看文件大小。",
            ("workspace.get_file_info",),
        ),
        (
            "调用 workspace.create_file，然后再回复。",
            ("workspace.create_file",),
        ),
        (
            "Please only use workspace.create_file and then reply.",
            ("workspace.create_file",),
        ),
        (
            "Execute the tool workspace.create_file now.",
            ("workspace.create_file",),
        ),
    ],
)
def test_high_confidence_directives_are_detected(manifests, goal, expected):
    assert find_explicitly_requested_tools(goal, manifests) == expected


@pytest.mark.parametrize(
    "goal",
    [
        "如何使用 workspace.create_file？请只解释，不要执行。",
        "为什么系统会调用 workspace.create_file？",
        "请解释为什么只调用 workspace.create_file 而不用其他工具。",
        "workspace.create_file 是什么？",
        "请说明是否需要使用 workspace.create_file。",
        "普通对话，不涉及工具。",
    ],
)
def test_explanatory_mentions_do_not_require_execution(manifests, goal):
    assert find_explicitly_requested_tools(goal, manifests) == ()


def test_disabled_and_unknown_tools_are_not_requirements(manifests):
    goal = "请调用 workspace.disabled，并调用 workspace.unknown。"

    assert find_explicitly_requested_tools(goal, manifests) == ()


@pytest.mark.parametrize(
    "goal",
    [
        (
            "G2 d6053a5 Redis 状态丢失恢复：请创建 "
            "tmp/rc1-g2/redis-d6053a5.txt，内容必须且只能是 rc1-redis-d6053a5，"
            "不要调用其他工具。"
        ),
        "请在当前工作区创建 tmp/report.json，内容为 {}。",
        "创建 notes/result.md，然后告诉我结果。",
        "Please create the file tmp/result.txt with the supplied content.",
    ],
)
def test_natural_language_file_creation_with_explicit_path_requires_tool(manifests, goal):
    assert find_required_goal_tools(goal, manifests) == ("workspace.create_file",)


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        (
            "创建 notes/existing.md，写入‘新版本’，如果已存在就先告诉我，不要覆盖。",
            "notes/existing.md",
        ),
        (
            "Create reports/summary.md; if it already exists, tell me and do not overwrite it.",
            "reports/summary.md",
        ),
        ("创建 notes/a.md，不要覆盖。", ""),
        ("如果已存在就不要覆盖，但请创建 notes/a.md 和 notes/b.md。", ""),
        ("如果已存在就不要覆盖，请创建 ../outside.md。", ""),
    ],
)
def test_conditional_no_overwrite_target_is_narrow_and_unambiguous(goal, expected):
    assert conditional_no_overwrite_target(goal) == expected


def test_confirmed_workspace_target_requires_exact_trusted_tool_evidence():
    target = "notes/existing.md"
    exact = {
        "tool_name": "workspace.search_files",
        "ok": True,
        "model_action": {"arguments": {"path": "notes", "query": "existing.md"}},
        "data": {"matches": [{"path": target, "type": "file"}]},
    }
    fuzzy = {
        "tool_name": "workspace.search_files",
        "ok": True,
        "model_action": {"arguments": {"path": "notes", "query": "existing"}},
        "data": {"matches": [{"path": "notes/existing-copy.md", "type": "file"}]},
    }
    failed_create = {
        "tool_name": "workspace.create_file",
        "ok": False,
        "model_action": {"arguments": {"path": target}},
        "error": {"code": "PATH_ALREADY_EXISTS"},
    }

    assert has_confirmed_workspace_target([exact], target) is True
    assert has_confirmed_workspace_target([fuzzy], target) is False
    assert has_confirmed_workspace_target([failed_create], target) is True


@pytest.mark.parametrize(
    "goal",
    [
        "请解释如何创建 tmp/result.txt，不要执行。",
        "请不要创建 tmp/result.txt。",
        "为什么创建 tmp/result.txt 需要权限？",
        "请介绍创建文件时应该注意什么。",
        "The docs explain how to create tmp/result.txt without executing it.",
    ],
)
def test_explanatory_or_negative_file_creation_does_not_require_tool(manifests, goal):
    assert find_required_goal_tools(goal, manifests) == ()


def test_missing_evidence_only_accepts_exact_successful_tool_result():
    required = ("workspace.create_file", "workspace.get_file_info")
    observations = [
        {"tool_name": "workspace.create_file", "ok": True},
        {
            "tool_name": "workspace.get_file_info",
            "ok": False,
            "error": {"code": "PATH_NOT_FOUND"},
        },
        {"tool_name": "workspace.other", "ok": True},
    ]

    assert find_missing_successful_tool_evidence(required, observations) == (
        "workspace.get_file_info",
    )
    assert find_latest_failed_required_tool(required, observations) == {
        "tool_name": "workspace.get_file_info",
        "ok": False,
        "error": {"code": "PATH_NOT_FOUND"},
    }


def test_feedback_contains_only_missing_tool_names_and_required_action():
    feedback = build_effect_guard_feedback(("workspace.create_file",))

    assert "workspace.create_file" in feedback
    assert "成功 ToolResult" in feedback
    assert "不得直接声称操作成功" in feedback


@pytest.mark.parametrize(
    ("proposed", "expected"),
    [
        ("workspace.create_directory", "workspace.create_file"),
        ("workspace.move_path", "workspace.create_file"),
        ("workspace.delete_path", "workspace.create_file"),
        ("workspace.create_file", None),
        ("workspace.get_file_info", None),
    ],
)
def test_required_workspace_effect_mismatch_blocks_only_conflicting_effects(proposed, expected):
    assert (
        find_required_workspace_effect_mismatch(
            ("workspace.create_file",),
            proposed,
        )
        == expected
    )


def test_workspace_effect_mismatch_feedback_is_bounded_and_does_not_echo_content():
    feedback = build_workspace_effect_mismatch_feedback(
        expected_tool_name="workspace.create_file",
        proposed_tool_name="workspace.create_directory",
    )

    assert "授权前拒绝" in feedback
    assert "workspace.create_file" in feedback
    assert "workspace.create_directory" in feedback
    assert "notes/private.md" not in feedback


def test_multi_material_feedback_requires_progress_not_tool_name_switching():
    feedback = build_effect_guard_feedback(
        ("workspace 多材料正文覆盖（不同正文来源，或单一来源经二次独立发现确认）",)
    )

    assert "材料覆盖而非答案措辞" in feedback
    assert "从已读锚点扩展到相关父目录" in feedback
    assert "发现的每个相关文件" in feedback
    assert "保持相同query" in feedback.replace(" ", "")
    assert "改变或缩小path" in feedback.replace(" ", "")
    assert "零命中的文件名搜索" in feedback
    assert "重复读取同一文件" in feedback


@pytest.mark.parametrize(
    ("workspace", "observations", "expected"),
    [
        (
            {"evidence": "metadata", "action": "read", "ambiguity": "clear"},
            [{"tool_name": "workspace.list_files", "ok": True}],
            (),
        ),
        (
            {"evidence": "metadata", "action": "read", "ambiguity": "clear"},
            [{"tool_name": "workspace.get_file_info", "ok": True}],
            (),
        ),
        (
            {"evidence": "metadata", "action": "read", "ambiguity": "clear"},
            [{"tool_name": "workspace.read_file", "ok": True}],
            ("workspace 目录/元数据读取（list_files/get_file_info）",),
        ),
        (
            {"evidence": "metadata", "action": "read", "ambiguity": "clear"},
            [{"tool_name": "workspace.create_file", "ok": True}],
            (),
        ),
        (
            {"evidence": "required", "action": "read", "ambiguity": "clear"},
            [{"tool_name": "workspace.list_files", "ok": True}],
            (
                "workspace 文件正文读取（read_file/read_files，或明确正文搜索任务的 search_text）",
            ),
        ),
        (
            {"evidence": "required", "action": "read", "ambiguity": "clear"},
            [{"tool_name": "workspace.create_file", "ok": True}],
            (
                "workspace 文件正文读取（read_file/read_files，或明确正文搜索任务的 search_text）",
            ),
        ),
        (
            {"evidence": "required", "action": "read", "ambiguity": "clear"},
            [{"tool_name": "workspace.read_file", "ok": True}],
            (),
        ),
        (
            {"evidence": "required", "action": "read", "ambiguity": "clear"},
            [{"tool_name": "workspace.read_files", "ok": True}],
            (),
        ),
        (
            {"evidence": "skip", "action": "write", "ambiguity": "clear"},
            [{"tool_name": "workspace.create_directory", "ok": True}],
            (),
        ),
        (
            {"evidence": "skip", "action": "destructive", "ambiguity": "clear"},
            [],
            ("workspace.delete_path",),
        ),
        (
            {
                "evidence": "skip",
                "action": "destructive",
                "ambiguity": "clarification_required",
            },
            [],
            (),
        ),
    ],
)
def test_structured_workspace_semantics_require_category_evidence(
    workspace, observations, expected
):
    intent = {"workspace": workspace}
    assert find_missing_workspace_evidence(intent, observations) == expected


def test_explicit_content_search_accepts_search_text_as_direct_evidence():
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }

    assert find_missing_workspace_evidence(
        intent,
        [{"tool_name": "workspace.search_text", "ok": True}],
        user_goal=(
            "在 project 中搜索字符串 AUTH_TOKEN，并告诉我哪些文件包含这个变量名，"
            "但不要输出任何值。"
        ),
    ) == ()


def test_search_text_does_not_replace_full_file_read_evidence():
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }

    assert find_missing_workspace_evidence(
        intent,
        [{"tool_name": "workspace.search_text", "ok": True}],
        user_goal="读取 notes/today.md 并概括，不要修改。",
    ) == (
        "workspace 文件正文读取（read_file/read_files，或明确正文搜索任务的 search_text）",
    )


def test_optional_rag_does_not_override_workspace_content_task():
    intent = {
        "retrieval": {"mode": "retrieve", "document_scope": "all"},
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"},
    }

    assert requires_rag_search(intent) is False


def test_explicit_required_rag_can_coexist_with_workspace_content_task():
    intent = {
        "retrieval": {"mode": "required", "document_scope": "selected"},
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"},
    }

    assert requires_rag_search(intent) is True


def _workspace_observation(
    tool_name: str,
    *,
    path: str = "",
    query: str = "",
    matches: tuple[str, ...] = (),
    files: tuple[str, ...] = (),
    entries: tuple[str, ...] = (),
) -> dict:
    data: dict = {}
    if tool_name == "workspace.read_file":
        data = {"path": path, "content": "evidence"}
    elif tool_name == "workspace.read_files":
        data = {
            "files": [
                {"path": item, "ok": True, "content": "evidence"}
                for item in files
            ]
        }
    elif tool_name in {"workspace.search_text", "workspace.search_files"}:
        data = {"matches": [{"path": item} for item in matches]}
    elif tool_name == "workspace.list_files":
        data = {
            "entries": [
                {"name": item, "path": f"/trusted/workspace/{path}/{item}", "type": "file"}
                for item in entries
            ]
        }
    return {
        "tool_name": tool_name,
        "ok": True,
        "model_action": {
            "action_type": "call_tool",
            "tool_name": tool_name,
            "arguments": {"path": path or ".", "query": query},
        },
        "data": data,
    }


def test_collection_workspace_goal_rejects_finish_after_one_exact_match() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation(
            "workspace.search_text",
            query="REQ-42",
            matches=("requests/REQ-42.md",),
        ),
        _workspace_observation("workspace.read_file", path="requests/REQ-42.md"),
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal="请阅读相关材料，说明完整流程，并给出每一步的文件依据。",
    ) == (
        "workspace 多材料正文覆盖（不同正文来源，或单一来源经二次独立发现确认）",
    )


def test_collection_workspace_goal_accepts_two_distinct_read_sources() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation(
            "workspace.search_text",
            query="approval workflow",
            matches=("requests/REQ-42.md", "policy.md", "procedure.md"),
        ),
        _workspace_observation(
            "workspace.list_files",
            path="requests",
            entries=("REQ-42.md",),
        ),
        _workspace_observation(
            "workspace.read_files",
            files=("requests/REQ-42.md", "policy.md", "procedure.md"),
        ),
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal="核对申请记录与操作流程是否一致，并区分冲突和未知信息。",
    ) == ()


def test_collection_workspace_goal_does_not_count_two_fragments_of_same_file() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation("workspace.read_file", path="requests/REQ-42.md"),
        _workspace_observation("workspace.read_file", path="requests/REQ-42.md"),
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal="比较相关文件中的事实和冲突。",
    ) != ()


def test_collection_workspace_goal_allows_single_source_after_directory_enumeration() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation(
            "workspace.search_text",
            query="REQ-42",
            matches=("requests/REQ-42.md",),
        ),
        _workspace_observation("workspace.read_file", path="requests/REQ-42.md"),
        _workspace_observation(
            "workspace.list_files",
            path="requests",
            entries=("REQ-42.md",),
        ),
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal="请检查相关材料并说明完整流程。",
    ) == ()


def test_collection_workspace_goal_does_not_count_zero_match_filename_search_as_exhaustion() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation(
            "workspace.search_files",
            query="PR-2026-017",
            matches=("procurement/requests/PR-2026-017.md",),
        ),
        _workspace_observation(
            "workspace.read_file",
            path="procurement/requests/PR-2026-017.md",
        ),
        _workspace_observation(
            "workspace.search_files",
            path="procurement",
            query="审批",
            matches=(),
        ),
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal="请阅读相关的材料，说明从提交到执行的流程，每一步给出文件依据。",
    ) == (
        "workspace 多材料正文覆盖（不同正文来源，或单一来源经二次独立发现确认）",
    )


def test_collection_workspace_goal_requires_every_discovered_file_before_finish() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation(
            "workspace.search_files",
            query="PR-2026-017",
            matches=("procurement/requests/PR-2026-017.md",),
        ),
        _workspace_observation(
            "workspace.list_files",
            path="procurement",
            entries=("policy.md", "procedure.md"),
        ),
        _workspace_observation(
            "workspace.read_files",
            files=("procurement/requests/PR-2026-017.md", "procurement/policy.md"),
        ),
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal="请阅读相关的材料，说明从提交到执行的流程，每一步给出文件依据。",
    ) != ()


def test_collection_workspace_goal_accepts_broad_nonzero_file_discovery_when_all_files_are_read() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation(
            "workspace.search_files",
            query="PR-2026-017",
            matches=("procurement/requests/PR-2026-017.md",),
        ),
        _workspace_observation(
            "workspace.search_files",
            query="procurement",
            matches=(
                "procurement/policy.md",
                "procurement/procedure.md",
                "procurement/requests/PR-2026-017.md",
            ),
        ),
        _workspace_observation(
            "workspace.read_files",
            files=(
                "procurement/policy.md",
                "procurement/procedure.md",
                "procurement/requests/PR-2026-017.md",
            ),
        ),
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal="请阅读相关的材料，说明从提交到执行的流程，每一步给出文件依据。",
    ) == ()


def test_collection_workspace_goal_does_not_count_same_query_with_another_tool() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation(
            "workspace.search_files",
            query="REQ-42",
            matches=("requests/REQ-42.md",),
        ),
        _workspace_observation("workspace.read_file", path="requests/REQ-42.md"),
        _workspace_observation(
            "workspace.search_text",
            query="REQ-42",
            matches=("requests/REQ-42.md",),
        ),
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal="请阅读相关材料，说明完整流程，并给出每一步的文件依据。",
    ) != ()


def test_collection_workspace_goal_does_not_count_same_query_in_narrower_path() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation(
            "workspace.search_text",
            path=".",
            query="PR-2026-017",
            matches=("procurement/requests/PR-2026-017.md",),
        ),
        _workspace_observation(
            "workspace.read_file",
            path="procurement/requests/PR-2026-017.md",
        ),
        _workspace_observation(
            "workspace.search_files",
            path="procurement",
            query="2026-017",
            matches=("procurement/requests/PR-2026-017.md",),
        ),
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal=(
            "请阅读与采购申请相关的材料，说明从提交到执行的完整流程，"
            "并为每一步提供文件依据。"
        ),
    ) == (
        "workspace 多材料正文覆盖（不同正文来源，或单一来源经二次独立发现确认）",
    )


def test_single_named_file_goal_keeps_one_file_evidence_contract() -> None:
    intent = {
        "workspace": {"evidence": "required", "action": "read", "ambiguity": "clear"}
    }
    observations = [
        _workspace_observation("workspace.read_file", path="reports/monthly.md")
    ]

    assert find_missing_workspace_evidence(
        intent,
        observations,
        user_goal="请核对 reports/monthly.md 中的金额并说明依据。",
    ) == ()
