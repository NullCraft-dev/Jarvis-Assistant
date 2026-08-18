from __future__ import annotations

from jarvis_worker.agent.core.evidence_navigation import (
    build_workspace_evidence_navigation_feedback,
    build_workspace_source_action_guard_feedback,
    build_workspace_source_chain_coverage,
    build_workspace_source_chain_feedback,
    build_workspace_source_evidence_ledger,
    evaluate_workspace_source_action_guard,
    sanitize_source_navigation_guard_details,
    workspace_source_chain_requires_more_evidence,
)


def _search(*paths: str) -> dict:
    return {
        "tool_name": "workspace.search_text",
        "ok": True,
        "data": {
            "matches": [{"path": path, "line_number": 10} for path in paths],
            "source_only": True,
        },
    }


def _read(path: str) -> dict:
    return {
        "tool_name": "workspace.read_file",
        "ok": True,
        "data": {
            "path": path,
            "content": _direct_edge_content(path),
            "start_line": 10,
            "end_line": 20,
            "total_lines": 100,
        },
    }


def _direct_edge_content(path: str) -> str:
    normalized = path.casefold()
    if "/web/" in normalized or normalized.startswith("apps/web"):
        return 'return apiPost<CreateTaskOutput>("/tasks", input)'
    if "gateway" in normalized:
        return "response = h.controlPlane.CreateTask(ctx, request)"
    if "control_plane" in normalized or "controlplane" in normalized:
        return "result = await task_svc.create_task(input_data)"
    if "outbox" in normalized or "publisher" in normalized:
        return 'EVENT_TO_STREAM = {"task.created": "jarvis:stream:run-queue"}'
    if "consumer" in normalized or "runtime_bus" in normalized:
        return 'client.xreadgroup(streams={STREAM_RUN_QUEUE: ">"})'
    if "worker" in normalized or "runner" in normalized or "executor" in normalized:
        return "self._process_job_with_cancel_check(job)"
    return "caller.invoke(target)"


def test_multiple_unread_candidates_trigger_batch_range_read_stage() -> None:
    feedback = build_workspace_evidence_navigation_feedback(
        [_search("src/a.py", "src/b.py", "src/c.py")]
    )

    assert feedback is not None
    assert "3 个尚未读取的候选文件" in feedback
    assert "workspace.read_files" in feedback
    assert "path 必须原样复制 ToolResult" in feedback
    assert "src/a.py" not in feedback


def test_single_unread_candidate_triggers_targeted_single_read() -> None:
    feedback = build_workspace_evidence_navigation_feedback(
        [_search("src/a.py", "src/b.py"), _read("src/a.py")]
    )

    assert feedback is not None
    assert "还有 1 个已定位候选未读取" in feedback
    assert "start_line/max_lines" in feedback


def test_all_candidates_read_trigger_gap_check_instead_of_more_broad_search() -> None:
    feedback = build_workspace_evidence_navigation_feedback(
        [_search("src/a.py", "src/b.py"), _read("src/a.py"), _read("src/b.py")]
    )

    assert feedback is not None
    assert "当前搜索候选已读取" in feedback
    assert "只有明确缺口" in feedback

    chain = build_workspace_source_chain_feedback(
        [_search("src/a.py", "src/b.py"), _read("src/a.py"), _read("src/b.py")]
    )
    assert chain is not None
    assert "caller/producer" in chain
    assert "方法定义不等于调用边" in chain
    assert "外层循环/dispatch" in chain
    assert len(chain) < 800
    assert "src/a.py" not in chain


def test_batch_partial_failure_is_counted_without_exposing_dynamic_paths() -> None:
    feedback = build_workspace_evidence_navigation_feedback(
        [
            _search("src/a.py"),
            {
                "tool_name": "workspace.read_files",
                "ok": True,
                "data": {
                    "files": [
                        {"path": "src/a.py", "ok": True},
                        {"path": "DO_NOT_PROMOTE.py", "ok": False},
                    ]
                },
            },
        ]
    )

    assert feedback is not None
    assert "1 个批量条目失败" in feedback
    assert "DO_NOT_PROMOTE.py" not in feedback


def test_latest_batch_failure_requires_path_discovery_before_more_batch_reads() -> None:
    feedback = build_workspace_evidence_navigation_feedback(
        [
            _search("src/a.py", "src/b.py"),
            {
                "tool_name": "workspace.read_files",
                "ok": True,
                "data": {
                    "files": [
                        {"path": "src/a.py", "ok": True},
                        {"path": "GUESSED.py", "ok": False},
                    ]
                },
            },
        ]
    )

    assert feedback is not None
    assert "Workspace 取证纠错阶段" in feedback
    assert "workspace.search_files" in feedback
    assert "GUESSED.py" not in feedback


def test_path_failure_prefers_bounded_suggestions_without_promoting_paths() -> None:
    feedback = build_workspace_evidence_navigation_feedback(
        [
            {
                "tool_name": "workspace.read_file",
                "ok": False,
                "data": {
                    "requested_path": "src/application/task_service.py",
                    "suggested_paths": ["src/runtime/tasks/service.py"],
                },
            }
        ]
    )

    assert feedback is not None
    assert "1 个有界已存在候选" in feedback
    assert "suggested_paths" in feedback
    assert "src/runtime/tasks/service.py" not in feedback


def test_successful_filename_search_clears_latest_batch_correction_stage() -> None:
    feedback = build_workspace_evidence_navigation_feedback(
        [
            _search("src/a.py", "src/b.py"),
            {
                "tool_name": "workspace.read_files",
                "ok": False,
                "data": {"files": [{"path": "GUESSED.py", "ok": False}]},
            },
            {
                "tool_name": "workspace.search_files",
                "ok": True,
                "data": {"matches": [{"path": "src/found.py"}]},
            },
        ]
    )

    assert feedback is not None
    assert "Workspace 取证纠错阶段" not in feedback
    assert "workspace.read_files" in feedback


def test_no_search_observation_has_no_navigation_feedback() -> None:
    assert build_workspace_evidence_navigation_feedback([_read("src/a.py")]) is None


def test_closing_budget_prioritizes_uncovered_facets_over_deeper_rereads() -> None:
    feedback = build_workspace_evidence_navigation_feedback(
        [_search("src/a.py", "src/b.py"), _read("src/a.py")],
        remaining_calls=4,
    )

    assert feedback is not None
    assert "取证预算进入收口窗口" in feedback
    assert "尚未取证的目标部分" in feedback

    chain = build_workspace_source_chain_feedback(
        [_search("src/a.py", "src/b.py"), _read("src/a.py")],
        user_goal="请阅读源码，说明从 Web 到 Worker 的端到端执行路径。",
        remaining_calls=4,
    )
    assert chain is not None
    assert "覆盖预算保护窗口已开启" in chain
    assert "顺序不受限制" in chain


def test_non_source_workspace_research_does_not_build_source_chain_feedback() -> None:
    observations = (
        [
            {
                "tool_name": "workspace.search_text",
                "ok": True,
                "data": {
                    "matches": [{"path": "notes/design.md", "line_number": 10}],
                    "source_only": False,
                },
            },
            _read("notes/design.md"),
        ]
    )

    assert build_workspace_evidence_navigation_feedback(observations) is not None
    assert build_workspace_source_chain_feedback(observations) is None
    assert build_workspace_source_evidence_ledger(observations) is None


def test_source_ledger_activates_from_read_source_path_without_source_only_search() -> None:
    ledger = build_workspace_source_evidence_ledger([_read("src/runtime/worker.py")])

    assert ledger is not None
    assert ledger["unique_source_paths"] == 1
    assert ledger["entries"][0]["path"] == "src/runtime/worker.py"
    assert "_process_job_with_cancel_check" in ledger["entries"][0]["fragments"][0]["excerpt"]
    assert "evidence_text" not in ledger["entries"][0]["fragments"][0]


def test_source_ledger_keeps_first_and_last_layers_and_counts_repeated_paths() -> None:
    observations = [_read(f"layer/{index}.py") for index in range(12)]
    observations.append(_read("layer/11.py"))

    ledger = build_workspace_source_evidence_ledger(observations)

    assert ledger is not None
    paths = [entry["path"] for entry in ledger["entries"]]
    assert paths == [
        "layer/0.py", "layer/1.py", "layer/2.py", "layer/3.py", "layer/4.py",
        "layer/7.py", "layer/8.py", "layer/9.py", "layer/10.py", "layer/11.py",
    ]
    assert ledger["omitted_source_paths"] == 2
    assert ledger["repeated_source_paths"] == 1


def test_source_ledger_excerpt_is_bounded_and_retains_head_middle_tail() -> None:
    observation = _read("src/large.py")
    observation["data"]["content"] = "H" * 1000 + "M" * 1000 + "T" * 1000

    ledger = build_workspace_source_evidence_ledger([observation])

    assert ledger is not None
    excerpt = ledger["entries"][0]["fragments"][0]["excerpt"]
    assert len(excerpt) <= 600
    assert "H" * 100 in excerpt
    assert "M" * 100 in excerpt
    assert "T" * 100 in excerpt


def test_source_ledger_retains_first_and_latest_fragments_for_revisited_path() -> None:
    first = _read("src/runtime/worker.py")
    first["data"].update(content="consumer loop evidence", start_line=200, end_line=260)
    latest = _read("src/runtime/worker.py")
    latest["data"].update(content="executor call evidence", start_line=560, end_line=620)

    ledger = build_workspace_source_evidence_ledger([first, latest])

    assert ledger is not None
    entry = ledger["entries"][0]
    assert entry["read_count"] == 2
    assert [fragment["start_line"] for fragment in entry["fragments"]] == [200, 560]
    assert "consumer loop" in entry["fragments"][0]["excerpt"]
    assert "executor call" in entry["fragments"][1]["excerpt"]


def test_cross_layer_coverage_detects_missing_frontend_endpoint_and_entry_stage() -> None:
    goal = (
        "请阅读这个代码库，说明 Web 创建任务后直到 Worker 开始执行的真实调用链。"
        "每一层都给出文件依据。"
    )
    observations = [
        _read("apps/agent-worker/src/runtime/outbox/publisher.py"),
        _read("apps/agent-worker/src/runtime_bus/consumer.py"),
        _read("apps/agent-worker/src/runtime/worker.py"),
    ]

    coverage = build_workspace_source_chain_coverage(goal, observations)

    assert coverage is not None
    assert coverage["covered_endpoint_count"] == 1
    assert coverage["missing_endpoints"] == ("frontend",)
    assert coverage["missing_stages"] == ("entry",)
    assert coverage["complete"] is False
    assert workspace_source_chain_requires_more_evidence(goal, observations) is True

    feedback = build_workspace_source_chain_feedback(
        observations,
        user_goal=goal,
        remaining_calls=3,
    )
    assert feedback is not None
    assert "运行端已覆盖 1/2" in feedback
    assert "入口/传输/执行阶段已覆盖 2/3" in feedback
    assert "Web/前端入口" in feedback
    assert "入口" in feedback
    assert "不得 finish" in feedback
    assert "apps/agent-worker" not in feedback


def test_cross_layer_coverage_detects_gateway_only_half_chain() -> None:
    goal = "Read this codebase and explain the real call chain from Web to Worker."
    observations = [
        _read("apps/gateway/internal/app/routes.go"),
        _read("apps/gateway/internal/api/handlers/task.go"),
        _read("apps/gateway/internal/controlplane/tasks.go"),
    ]

    coverage = build_workspace_source_chain_coverage(goal, observations)

    assert coverage is not None
    assert coverage["covered_endpoint_count"] == 0
    assert coverage["missing_endpoints"] == ("frontend", "worker")
    assert coverage["missing_stages"] == ("entry", "transport", "execution")
    assert coverage["complete"] is False


def test_control_plane_under_agent_worker_does_not_impersonate_worker_endpoint() -> None:
    goal = (
        "请阅读这个代码库，说明 Web、Gateway、Control Plane 到 Worker 的端到端源码调用链。"
    )
    observations = [
        _read("apps/web/src/api/tasks.ts"),
        _read("apps/gateway/internal/api/handlers/task.go"),
        _read("apps/agent-worker/src/jarvis_worker/control_plane/app.py"),
    ]

    coverage = build_workspace_source_chain_coverage(goal, observations)

    assert coverage is not None
    assert coverage["missing_endpoints"] == ("worker",)
    assert coverage["missing_stages"] == ("transport", "execution")


def test_source_action_guard_allows_distinct_read_in_already_covered_component() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [_read("apps/agent-worker/src/runtime/worker.py")]

    feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.read_file",
        arguments={"path": "apps/agent-worker/src/runtime/runner.py"},
    )

    assert feedback is None


def test_source_action_guard_allows_missing_endpoint_and_root_search() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [_read("apps/agent-worker/src/runtime/worker.py")]

    frontend_read = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.read_file",
        arguments={"path": "apps/web/src/api/tasks.ts"},
    )
    root_search = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": ".", "query": "CreateTask"},
    )

    assert frontend_read is None
    assert root_search is None


def test_source_action_guard_allows_productive_discovery_and_read_in_any_order() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [
        {
            "tool_name": "workspace.list_files",
            "ok": True,
            "data": {
                "entries": [
                    {"path": "apps/web/src/api/client.ts", "type": "file"},
                ]
            },
        },
        _search("apps/gateway/internal/app/routes.go"),
        _search("apps/agent-worker/src/jarvis_worker/runtime/worker.py"),
    ]

    feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": "apps", "query": "dispatch"},
    )
    read_feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.read_files",
        arguments={"files": ["apps/web/src/api/client.ts"]},
    )

    assert feedback is None
    assert read_feedback is None


def test_directory_navigation_does_not_block_first_targeted_endpoint_search() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [
        {
            "tool_name": "workspace.list_files",
            "ok": True,
            "data": {"entries": [{"path": path, "type": "dir"}]},
        }
        for path in ("apps", "apps/web", "apps/web/src")
    ]

    feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": "apps/web/src", "query": "createTask"},
    )

    assert feedback is None


def test_source_action_guard_allows_any_missing_slot_before_first_read() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"

    worker_feedback = build_workspace_source_action_guard_feedback(
        goal,
        [],
        tool_name="workspace.read_file",
        arguments={"path": "apps/agent-worker/src/runtime/worker.py"},
    )
    frontend_feedback = build_workspace_source_action_guard_feedback(
        goal,
        [],
        tool_name="workspace.search_text",
        arguments={"path": "apps/web/src", "query": "createTask"},
    )

    assert worker_feedback is None
    assert frontend_feedback is None


def test_source_action_guard_allows_productive_file_info_discovery() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [
        {
            "tool_name": "workspace.get_file_info",
            "ok": True,
            "data": {"path": f"src/layer_{index}.py", "type": "file"},
        }
        for index in range(3)
    ]

    feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.list_files",
        arguments={"path": "src"},
    )

    assert feedback is None


def test_source_action_guard_does_not_infer_no_progress_from_component_category() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [_read("apps/web/src/api/client.ts")]

    list_feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.list_files",
        arguments={"path": "apps/web/src/api"},
    )
    covered_query_feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.search_files",
        arguments={"path": ".", "query": "web"},
    )
    missing_query_feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.search_files",
        arguments={"path": "apps", "query": "worker"},
    )
    worker_container_feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.list_files",
        arguments={"path": "apps/agent-worker"},
    )

    assert list_feedback is None
    assert covered_query_feedback is None
    assert missing_query_feedback is None
    assert worker_container_feedback is None


def test_source_action_guard_rejects_exact_repeat_of_successful_action() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [
        {
            "tool_name": "workspace.search_text",
            "model_action": {
                "tool_name": "workspace.search_text",
                "arguments": {"path": "apps", "query": "createTask", "max_results": 30},
            },
            "ok": True,
            "data": {"matches": [{"path": "apps/web/src/stores/taskStore.ts"}]},
        }
    ]

    decision = evaluate_workspace_source_action_guard(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": "apps", "query": "createTask", "max_results": 30},
    )

    assert decision is not None
    assert decision.diagnostics["reason_code"] == "REPEATED_SOURCE_ACTION"
    assert decision.diagnostics["tool_class"] == "discovery"
    assert "路径" not in str(decision.diagnostics)
    assert "createTask" not in str(decision.diagnostics)


def test_source_action_guard_allows_reproduction_progressive_discovery_path() -> None:
    goal = "请阅读这个代码库，说明 Web 创建任务后直到 Worker 开始执行的真实调用链。"
    observations = [
        {
            "tool_name": "workspace.list_files",
            "ok": True,
            "data": {"entries": [{"path": "apps", "type": "dir"}]},
        },
        _search("packages/contracts/src/task.ts"),
        _search(
            "apps/web/src/stores/taskStore.ts",
            "apps/gateway/internal/api/handlers/task.go",
        ),
    ]

    search_decision = evaluate_workspace_source_action_guard(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": "apps/gateway", "query": "CreateTask"},
    )
    read_decision = evaluate_workspace_source_action_guard(
        goal,
        observations,
        tool_name="workspace.read_files",
        arguments={
            "files": [
                "apps/web/src/stores/taskStore.ts",
                "apps/gateway/internal/api/handlers/task.go",
            ]
        },
    )

    assert search_decision is None
    assert read_decision is None


def test_source_action_guard_rejects_only_after_repeated_discovery_without_new_candidates() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [
        _search("apps/web/src/api/tasks.ts"),
        _search("apps/web/src/api/tasks.ts"),
        _search("apps/web/src/api/tasks.ts"),
    ]

    decision = evaluate_workspace_source_action_guard(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": "apps", "query": "differentSymbol"},
    )

    assert decision is not None
    assert decision.diagnostics["reason_code"] == "DISCOVERY_NO_PROGRESS"
    assert decision.diagnostics["productive_discovery_count"] == 1
    assert decision.diagnostics["nonprogress_discovery_streak"] == 2
    assert decision.diagnostics["unique_candidate_count"] == 1


def test_source_action_guard_protects_remaining_budget_by_missing_category() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [
        _read("apps/web/src/api/tasks.ts"),
        _read("apps/gateway/internal/api/handlers/task.go"),
        _read("runtime/outbox/publisher.py"),
    ]

    rejected = evaluate_workspace_source_action_guard(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": "apps/gateway", "query": "CreateTask"},
        remaining_calls=4,
    )
    consumer_allowed = evaluate_workspace_source_action_guard(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": "runtime/consumer", "query": "xreadgroup"},
        remaining_calls=4,
    )
    worker_allowed = evaluate_workspace_source_action_guard(
        goal,
        observations,
        tool_name="workspace.search_files",
        arguments={"path": "apps/agent-worker", "query": "worker"},
        remaining_calls=4,
    )
    batch_allowed = evaluate_workspace_source_action_guard(
        goal,
        observations,
        tool_name="workspace.read_files",
        arguments={"files": ["runtime/consumer.py", "runtime/worker.py"]},
        remaining_calls=4,
    )

    assert rejected is not None
    assert rejected.diagnostics["reason_code"] == "COVERAGE_BUDGET_AT_RISK"
    assert rejected.diagnostics["remaining_call_count"] == 4
    assert rejected.diagnostics["coverage_budget_threshold"] == 4
    assert rejected.diagnostics["coverage_budget_at_risk"] is True
    assert consumer_allowed is None
    assert worker_allowed is None
    assert batch_allowed is None


def test_source_action_guard_keeps_unclassified_discovery_before_budget_window() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [_read("apps/web/src/api/tasks.ts")]

    decision = evaluate_workspace_source_action_guard(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": ".", "query": "dispatch"},
        remaining_calls=7,
    )

    assert decision is None


def test_source_navigation_diagnostics_sanitizer_drops_dynamic_fields() -> None:
    details = sanitize_source_navigation_guard_details(
        {
            "policy_version": "source-navigation-v5",
            "reason_code": "DISCOVERY_NO_PROGRESS",
            "tool_class": "discovery",
            "missing_slot_count": 4,
            "proposed_slot_count": 1,
            "proposed_missing_slot_count": 1,
            "discovery_count_since_read": 3,
            "productive_discovery_count": 1,
            "nonprogress_discovery_streak": 2,
            "unique_candidate_count": 30,
            "has_actionable_candidates": True,
            "remaining_call_count": 4,
            "coverage_budget_threshold": 6,
            "coverage_budget_at_risk": True,
            "path": "apps/private/secret.py",
            "query": "secret",
            "feedback": "dynamic",
        }
    )

    assert details is not None
    assert details["reason_code"] == "DISCOVERY_NO_PROGRESS"
    assert details["unique_candidate_count"] == 30
    assert details["coverage_budget_at_risk"] is True
    assert "path" not in details
    assert "query" not in details
    assert "feedback" not in details


def test_cross_layer_coverage_closes_only_after_endpoints_and_middle_stage() -> None:
    goal = "请阅读源码，说明从 Web 到 Worker 的端到端执行路径。"
    observations = [
        _read("apps/web/src/api/tasks.ts"),
        _read("apps/gateway/internal/api/handlers/task.go"),
        _read("apps/agent-worker/src/database/outbox/publisher.py"),
        _read("apps/agent-worker/src/runtime_bus/consumer.py"),
        _read("apps/agent-worker/src/runtime/worker.py"),
    ]

    coverage = build_workspace_source_chain_coverage(goal, observations)

    assert coverage is not None
    assert coverage["covered_endpoint_count"] == 2
    assert coverage["covered_stage_count"] == 3
    assert coverage["complete"] is True
    assert workspace_source_chain_requires_more_evidence(goal, observations) is False


def test_path_taxonomy_does_not_close_unrelated_frontend_or_worker_claim_edges() -> None:
    goal = "请阅读源码，说明从 Web 创建任务到 Worker 开始执行的端到端调用链。"
    observations = [
        {
            "tool_name": "workspace.read_file",
            "ok": True,
            "data": {
                "path": "apps/web/src/views/ScheduleView.vue",
                "content": "await scheduleStore.createScheduledTask(input)",
            },
        },
        _read("apps/gateway/internal/api/handlers/task.go"),
        _read("apps/agent-worker/src/database/outbox/publisher.py"),
        _read("apps/agent-worker/src/runtime_bus/consumer.py"),
        {
            "tool_name": "workspace.read_file",
            "ok": True,
            "data": {
                "path": "apps/agent-worker/src/runtime/worker.py",
                "content": "disposition = self._claim_job(job)",
            },
        },
    ]

    coverage = build_workspace_source_chain_coverage(goal, observations)

    assert coverage is not None
    assert coverage["schema"] == "workspace-source-chain-coverage-v3"
    assert coverage["missing_endpoints"] == ("frontend", "worker")
    assert coverage["missing_stages"] == ("entry", "execution")
    assert coverage["missing_evidence_slots"] == (
        "endpoint:frontend",
        "endpoint:worker",
    )
    assert coverage["complete"] is False


def test_single_module_source_chain_does_not_enable_cross_runtime_hard_gate() -> None:
    goal = "请阅读源码，说明这个函数内部的调用链。"
    observations = [_read("src/service.py")]

    assert build_workspace_source_chain_coverage(goal, observations) is None
    assert workspace_source_chain_requires_more_evidence(goal, observations) is False


def test_single_module_source_goal_is_not_subject_to_discovery_or_category_guard() -> None:
    goal = "请阅读源码，说明这个函数内部的调用链。"
    observations = [
        _search("src/service.py"),
        _search("src/repository.py"),
        _search("src/models.py"),
    ]

    feedback = build_workspace_source_action_guard_feedback(
        goal,
        observations,
        tool_name="workspace.search_text",
        arguments={"path": "src", "query": "load_record"},
    )

    assert feedback is None
