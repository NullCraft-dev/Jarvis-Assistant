"""Workspace 正文取证的确定性进度反馈。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

_MAX_TRACKED_PATHS = 100
_MAX_LEDGER_PATHS = 10
_MAX_LEDGER_EXCERPT_CHARS = 600
_SOURCE_SUFFIXES = frozenset(
    {
        ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
        ".jsx", ".kt", ".kts", ".mjs", ".php", ".py", ".rb", ".rs", ".sh",
        ".swift", ".ts", ".tsx", ".vue",
    }
)
_SOURCE_CHAIN_CODE_RE = re.compile(
    r"(?:代码|源码|代码库|文件依据|调用点|implementation|source\s+code|codebase)",
    re.IGNORECASE,
)
_SOURCE_CHAIN_RELATION_RE = re.compile(
    r"(?:调用链|真实链路|执行链|执行路径|数据流|每一层|端到端|直到|"
    r"\bcall\s+chain\b|\bexecution\s+path\b|\bdata\s+flow\b|\bend[- ]to[- ]end\b|"
    r"\bfrom\b.{0,120}\bto\b)",
    re.IGNORECASE | re.DOTALL,
)
_ENDPOINT_GOAL_PATTERNS = {
    "frontend": re.compile(
        r"(?:\bweb\b|\bfrontend\b|\bui\b|\bbrowser\b|前端|网页|浏览器|界面)",
        re.IGNORECASE,
    ),
    "gateway": re.compile(r"(?:\bgateway\b|网关)", re.IGNORECASE),
    "control_plane": re.compile(
        r"(?:\bcontrol[-_ ]?plane\b|控制面)", re.IGNORECASE
    ),
    "worker": re.compile(
        r"(?:\bworker\b|\bconsumer\b|\bexecutor\b|\brunner\b|"
        r"工作进程|消费者|执行器|代理循环|Agent\s+loop)",
        re.IGNORECASE,
    ),
}
_ENDPOINT_PATH_TOKENS = {
    "frontend": frozenset({"web", "frontend", "ui", "browser", "renderer"}),
    "gateway": frozenset({"gateway"}),
    "control_plane": frozenset({"controlplane", "control_plane"}),
    "worker": frozenset({"worker", "consumer", "executor", "runner"}),
}
_STAGE_PATH_TOKENS = {
    "entry": frozenset(
        {
            "web", "frontend", "ui", "browser", "renderer", "route", "routes",
            "router", "handler", "handlers", "controller", "controllers", "api",
        }
    ),
    "transport": frozenset(
        {
            "gateway", "controlplane", "control_plane", "client", "transport",
            "queue", "redis", "outbox", "publisher", "bus", "broker", "producer",
        }
    ),
    "execution": frozenset(
        {"worker", "consumer", "dispatcher", "executor", "runner", "loop"}
    ),
}
_ENDPOINT_DIRECT_EVIDENCE_PATTERNS = {
    "frontend": re.compile(
        r"(?:\b(?:fetch|axios|request)\s*\(|"
        r"\bapi(?:Get|Post|Put|Patch|Delete|Request)\s*(?:<[^>]{1,200}>)?\s*\(|"
        r"\b(?:api|client|transport)\s*\.\s*[A-Za-z_]\w*\s*\()",
        re.IGNORECASE,
    ),
    "gateway": re.compile(
        r"\b(?:[A-Za-z_]\w*\s*\.\s*)*"
        r"[A-Za-z_]*?(?:controlPlane|client|service|runtime|orchestrator)"
        r"[A-Za-z_]*\s*\.\s*[A-Za-z_]\w*\s*\(",
        re.IGNORECASE,
    ),
    "control_plane": re.compile(
        r"\b(?:[A-Za-z_]\w*\s*\.\s*)*"
        r"[A-Za-z_]*?(?:service|svc|publisher|queue|outbox)[A-Za-z_]*"
        r"\s*\.\s*[A-Za-z_]\w*\s*\(",
        re.IGNORECASE,
    ),
    "worker": re.compile(
        r"(?:\bself\s*\.\s*_process_job_with_cancel_check\s*\(|"
        r"\b(?:[A-Za-z_]\w*\s*\.\s*)*"
        r"[A-Za-z_]*?(?:run_executor|runner|executor|agent_loop)[A-Za-z_]*"
        r"\s*\.\s*(?:run|execute|invoke|start)[A-Za-z_]*\s*\()",
        re.IGNORECASE,
    ),
}
_TRANSPORT_PRODUCER_EVIDENCE_RE = re.compile(
    r"(?:\bXADD\b|\bxadd\s*\(|\bEVENT_TO_STREAM\b|"
    r"\b(?:publish|enqueue|produce|send)[A-Za-z_]*\s*\()",
    re.IGNORECASE,
)
_TRANSPORT_CONSUMER_EVIDENCE_RE = re.compile(
    r"(?:\bXREADGROUP\b|\bxreadgroup\s*\(|"
    r"\b(?:consume|dequeue|subscribe|read_delivery)[A-Za-z_]*\s*\()",
    re.IGNORECASE,
)
_ENDPOINT_LABELS = {
    "frontend": "Web/前端入口",
    "gateway": "Gateway/网关",
    "control_plane": "Control Plane/控制面",
    "worker": "Worker/消费执行端",
}
_STAGE_LABELS = {
    "entry": "入口",
    "transport": "传输/跨层交接",
    "execution": "消费/执行",
}
_PRODUCER_NAVIGATION_RE = re.compile(
    r"(?:\bxadd\b|event[_-]?to[_-]?stream|outbox|publish|producer|enqueue)",
    re.IGNORECASE,
)
_CONSUMER_NAVIGATION_RE = re.compile(
    r"(?:\bxreadgroup\b|read[_-]?delivery|consumer|consume|subscriber|dequeue)",
    re.IGNORECASE,
)
_SOURCE_NAVIGATION_TOOLS = frozenset(
    {
        "workspace.get_file_info",
        "workspace.list_files",
        "workspace.search_text",
        "workspace.search_files",
        "workspace.read_file",
        "workspace.read_files",
    }
)
_SOURCE_DISCOVERY_TOOLS = frozenset(
    {
        "workspace.get_file_info",
        "workspace.list_files",
        "workspace.search_text",
        "workspace.search_files",
    }
)
_MAX_CONSECUTIVE_NONPROGRESS_DISCOVERIES = 2
_NAVIGATION_WORKER_CONTAINER_TOKENS = frozenset(
    {"agent-worker", "agent_worker", "jarvis_worker"}
)
_SOURCE_NAVIGATION_POLICY_VERSION = "source-navigation-v5"
_SOURCE_NAVIGATION_REASON_CODES = frozenset(
    {
        "REPEATED_SOURCE_ACTION",
        "DISCOVERY_NO_PROGRESS",
        "COVERAGE_BUDGET_AT_RISK",
    }
)
_SOURCE_NAVIGATION_TOOL_CLASSES = frozenset({"discovery", "read", "navigation"})


@dataclass(frozen=True)
class SourceActionGuardDecision:
    """工具执行前的安全导航退回；diagnostics 不含动态路径、查询或正文。"""

    feedback: str
    diagnostics: dict[str, Any]


def _path_tokens(path: str) -> frozenset[str]:
    """按路径段分类，避免仓库容器名冒充真实执行端证据。

    `apps/agent-worker/.../control_plane/app.py` 中的 `agent-worker` 只是项目目录，
    不能仅凭这个父目录就判定已经读取 Worker consumer/executor。文件名和其他路径段
    仍会按 `_`/`-` 拆分，因此 `run_executor.py` 与 `runtime_bus` 仍可正确归类。
    """
    tokens: set[str] = set()
    parts = PurePosixPath(path.casefold()).parts
    for index, raw_segment in enumerate(parts):
        segment = PurePosixPath(raw_segment).stem if index == len(parts) - 1 else raw_segment
        if segment in {"agent-worker", "agent_worker", "jarvis_worker"}:
            tokens.add(segment)
            continue
        normalized = segment.replace("control-plane", "controlplane").replace(
            "control_plane", "controlplane"
        )
        tokens.add(normalized)
        tokens.update(
            token for token in re.split(r"[^a-z0-9]+", normalized) if token
        )
    return frozenset(tokens)


def _classified_path_categories(path: str) -> tuple[frozenset[str], frozenset[str]]:
    tokens = _path_tokens(path)
    endpoints = frozenset(
        name
        for name, expected in _ENDPOINT_PATH_TOKENS.items()
        if tokens.intersection(expected)
    )
    stages = frozenset(
        name
        for name, expected in _STAGE_PATH_TOKENS.items()
        if tokens.intersection(expected)
    )
    return endpoints, stages


def _classified_navigation_categories(
    path: str,
) -> tuple[frozenset[str], frozenset[str]]:
    """导航目标可以使用项目容器定位端点，但不能把它当作已读正文证据。"""
    tokens = _path_tokens(path)
    endpoints, stages = _classified_path_categories(path)
    if tokens.intersection(_NAVIGATION_WORKER_CONTAINER_TOKENS):
        endpoints = frozenset((*endpoints, "worker"))
        stages = frozenset((*stages, "execution"))
    return endpoints, stages


def _classified_query_categories(query: object) -> tuple[frozenset[str], frozenset[str]]:
    if not isinstance(query, str) or not query.strip():
        return frozenset(), frozenset()
    normalized = query.casefold().replace("control-plane", "controlplane").replace(
        "control_plane", "controlplane"
    )
    tokens = frozenset(re.findall(r"[a-z0-9]+", normalized))
    endpoints = {
        name
        for name, pattern in _ENDPOINT_GOAL_PATTERNS.items()
        if pattern.search(query)
    }
    endpoints.update(
        name
        for name, expected in _ENDPOINT_PATH_TOKENS.items()
        if tokens.intersection(expected)
    )
    stages = {
        name
        for name, expected in _STAGE_PATH_TOKENS.items()
        if tokens.intersection(expected)
    }
    return frozenset(endpoints), frozenset(stages)


def _fixed_missing_labels(coverage: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        [
            _ENDPOINT_LABELS[name]
            for name in coverage["missing_endpoints"]
            if name in _ENDPOINT_LABELS
        ]
        + [
            _STAGE_LABELS[name]
            for name in coverage["missing_stages"]
            if name in _STAGE_LABELS
        ]
    )


def workspace_source_chain_missing_summary(coverage: dict[str, Any]) -> str:
    """只使用 Runtime 固定 taxonomy 生成缺口摘要，不提升不可信路径。"""
    labels = _fixed_missing_labels(coverage)
    return "、".join(labels) if labels else "无"


def _proposed_source_paths(tool_name: str, arguments: dict[str, Any]) -> tuple[str, ...]:
    if tool_name == "workspace.read_files":
        values = arguments.get("files")
        if not isinstance(values, list):
            return ()
        paths: list[str] = []
        for value in values:
            if isinstance(value, str):
                # read_files 支持 path:start:end 简写；行范围不参与类别判断。
                paths.append(re.sub(r":\d+:\d+$", "", value))
            elif isinstance(value, dict) and isinstance(value.get("path"), str):
                paths.append(value["path"])
            else:
                return ()
        return tuple(paths)
    path = arguments.get("path", ".")
    return (path,) if isinstance(path, str) else ()


def _discovery_since_latest_source_read(
    observations: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    latest_read = -1
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            continue
        if _successful_read_fragments((observation,)):
            latest_read = index
    return tuple(
        observation
        for observation in observations[latest_read + 1 :]
        if isinstance(observation, dict)
        and observation.get("ok") is True
        # 目录浏览只发现容器，不代表已经获得与当前证据槽相关的源码候选。
        # 只有正文/名称搜索和文件元信息进入 search→read 配额。
        and observation.get("tool_name")
        in {"workspace.search_text", "workspace.search_files", "workspace.get_file_info"}
    )


def _source_candidate_paths(observation: dict[str, Any]) -> frozenset[str]:
    if observation.get("ok") is not True:
        return frozenset()
    data = observation.get("data")
    if not isinstance(data, dict):
        return frozenset()
    tool_name = observation.get("tool_name")
    if tool_name in {"workspace.search_text", "workspace.search_files"}:
        candidates = data.get("matches")
    elif tool_name == "workspace.list_files":
        candidates = data.get("entries")
    elif tool_name == "workspace.get_file_info":
        candidates = [data] if data.get("type") == "file" else []
    else:
        candidates = []
    if not isinstance(candidates, list):
        return frozenset()
    return frozenset(
        str(candidate["path"])
        for candidate in candidates
        if isinstance(candidate, dict) and _is_source_path(candidate.get("path"))
    )


def _discovery_progress(
    observations: tuple[dict[str, Any], ...],
) -> dict[str, int | bool]:
    discoveries = _discovery_since_latest_source_read(observations)
    seen: set[str] = set()
    productive = 0
    nonprogress_streak = 0
    for observation in discoveries:
        candidates = set(_source_candidate_paths(observation))
        if candidates.difference(seen):
            productive += 1
            nonprogress_streak = 0
        else:
            nonprogress_streak += 1
        seen.update(candidates)
    return {
        "discovery_count_since_read": len(discoveries),
        "productive_discovery_count": productive,
        "nonprogress_discovery_streak": nonprogress_streak,
        "unique_candidate_count": len(seen),
        "has_actionable_candidates": bool(seen),
    }


def _normalized_action_arguments(arguments: object) -> object:
    if not isinstance(arguments, dict):
        return None
    return {
        key: value
        for key, value in arguments.items()
        if key != "workspace_root"
    }


def _repeats_successful_source_action(
    observations: tuple[dict[str, Any], ...],
    *,
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    expected = _normalized_action_arguments(arguments)
    for observation in reversed(observations):
        if not isinstance(observation, dict) or observation.get("ok") is not True:
            continue
        model_action = observation.get("model_action")
        if (
            observation.get("tool_name") == tool_name
            and isinstance(model_action, dict)
            and _normalized_action_arguments(model_action.get("arguments")) == expected
        ):
            return True
    return False


def _classified_navigation_slots(
    paths: Iterable[str], query: object
) -> tuple[frozenset[str], bool]:
    slots: set[str] = set()
    has_unclassified_target = False
    navigation_text: list[str] = []
    for path in paths:
        if path.strip() in {"", ".", "./"}:
            has_unclassified_target = True
            continue
        endpoints, _ = _classified_navigation_categories(path)
        if not endpoints:
            has_unclassified_target = True
        slots.update(f"endpoint:{endpoint}" for endpoint in endpoints)
        navigation_text.append(path)
    query_endpoints, _ = _classified_query_categories(query)
    slots.update(f"endpoint:{endpoint}" for endpoint in query_endpoints)
    if isinstance(query, str):
        navigation_text.append(query)
    combined = "\n".join(navigation_text)
    if _PRODUCER_NAVIGATION_RE.search(combined):
        slots.add("transport:producer")
    if _CONSUMER_NAVIGATION_RE.search(combined):
        slots.add("transport:consumer")
    return frozenset(slots), has_unclassified_target


def evaluate_workspace_source_action_guard(
    user_goal: object,
    observations: Iterable[dict[str, Any]],
    *,
    tool_name: str,
    arguments: dict[str, Any],
    slot_attempts: dict[str, int] | None = None,
    remaining_calls: int | None = None,
) -> SourceActionGuardDecision | None:
    """只阻止可证明无进展的动作，不规定证据面的调查顺序。

    `slot_attempts` 仅为旧 checkpoint/调用方兼容保留，不再选择唯一活动槽。
    常规阶段允许任一缺失证据面、未分类目标和新的源码读取自由推进；覆盖预算
    进入保护窗口后，发现动作必须指向任一缺失证据面。完全重复的成功动作或
    连续发现没有新增源码候选时同样有界退回。
    """
    del slot_attempts
    if tool_name not in _SOURCE_NAVIGATION_TOOLS:
        return None
    observation_items = tuple(observations)
    coverage = build_workspace_source_chain_coverage(user_goal, observation_items)
    if coverage is None or coverage["complete"] is True:
        return None

    paths = _proposed_source_paths(tool_name, arguments)
    if not paths:
        return None
    proposed_slots, _ = _classified_navigation_slots(
        paths, arguments.get("query")
    )
    missing_slots = frozenset(str(slot) for slot in coverage["missing_evidence_slots"])
    normalized_remaining = (
        remaining_calls
        if isinstance(remaining_calls, int)
        and not isinstance(remaining_calls, bool)
        and remaining_calls >= 0
        else None
    )
    coverage_budget_threshold = len(missing_slots) * 2
    coverage_budget_at_risk = (
        normalized_remaining is not None
        and normalized_remaining > 0
        and normalized_remaining <= coverage_budget_threshold
    )
    progress = _discovery_progress(observation_items)
    tool_class = (
        "discovery"
        if tool_name in _SOURCE_DISCOVERY_TOOLS
        else "read"
        if tool_name in {"workspace.read_file", "workspace.read_files"}
        else "navigation"
    )
    diagnostics: dict[str, Any] = {
        "policy_version": _SOURCE_NAVIGATION_POLICY_VERSION,
        "tool_class": tool_class,
        "missing_slot_count": len(missing_slots),
        "proposed_slot_count": len(proposed_slots),
        "proposed_missing_slot_count": len(proposed_slots.intersection(missing_slots)),
        "coverage_budget_threshold": coverage_budget_threshold,
        "coverage_budget_at_risk": coverage_budget_at_risk,
        **progress,
    }
    if normalized_remaining is not None:
        diagnostics["remaining_call_count"] = normalized_remaining

    if _repeats_successful_source_action(
        observation_items,
        tool_name=tool_name,
        arguments=arguments,
    ):
        diagnostics["reason_code"] = "REPEATED_SOURCE_ACTION"
        return SourceActionGuardDecision(
            feedback=(
                "跨层源码取证规划被 Runtime 退回：该源码动作与本次 Run 中已经成功执行的动作完全相同，"
                "不会产生新的候选或正文证据。请改用已有 ToolResult 中尚未读取的候选，或使用不同范围/"
                "标识符推进任一未覆盖证据面；不得把同一搜索当作分页。"
            ),
            diagnostics=diagnostics,
        )

    if coverage_budget_at_risk and (
        (proposed_slots and not proposed_slots.intersection(missing_slots))
        or (tool_name in _SOURCE_DISCOVERY_TOOLS and not proposed_slots)
    ):
        diagnostics["reason_code"] = "COVERAGE_BUDGET_AT_RISK"
        return SourceActionGuardDecision(
            feedback=(
                "跨层源码取证规划被 Runtime 退回：剩余工具预算已进入覆盖保护窗口，当前动作没有指向"
                f"任何未覆盖证据面。当前仍缺：{workspace_source_chain_missing_summary(coverage)}。"
                "可以自由选择其中任一证据面，或用 read_files 一次推进多个缺口；不得继续加深只属于已"
                "覆盖/非必需分类的组件。Runtime 不指定文件、符号或证据面的先后顺序。"
            ),
            diagnostics=diagnostics,
        )

    if (
        tool_name in _SOURCE_DISCOVERY_TOOLS
        and progress["nonprogress_discovery_streak"]
        >= _MAX_CONSECUTIVE_NONPROGRESS_DISCOVERIES
        and progress["has_actionable_candidates"] is True
    ):
        diagnostics["reason_code"] = "DISCOVERY_NO_PROGRESS"
        return SourceActionGuardDecision(
            feedback=(
                "跨层源码取证规划被 Runtime 退回：最近连续的发现动作没有增加源码候选，而已有候选仍可"
                "读取。下一步应从真实 ToolResult 选择尚未读取的候选执行 read_file/read_files，或使用能够"
                "推进任一未覆盖证据面的新范围；Runtime 不规定证据面的调查顺序。"
            ),
            diagnostics=diagnostics,
        )
    return None


def build_workspace_source_action_guard_feedback(
    user_goal: object,
    observations: Iterable[dict[str, Any]],
    *,
    tool_name: str,
    arguments: dict[str, Any],
    slot_attempts: dict[str, int] | None = None,
    remaining_calls: int | None = None,
) -> str | None:
    """兼容旧调用方的纯反馈投影。"""
    decision = evaluate_workspace_source_action_guard(
        user_goal,
        observations,
        tool_name=tool_name,
        arguments=arguments,
        slot_attempts=slot_attempts,
        remaining_calls=remaining_calls,
    )
    return decision.feedback if decision is not None else None


def sanitize_source_navigation_guard_details(value: object) -> dict[str, Any] | None:
    """在持久化边界重建固定导航诊断，拒绝路径、查询、正文等动态字段。"""
    if not isinstance(value, dict):
        return None
    policy_version = value.get("policy_version")
    reason_code = value.get("reason_code")
    tool_class = value.get("tool_class")
    if (
        policy_version != _SOURCE_NAVIGATION_POLICY_VERSION
        or reason_code not in _SOURCE_NAVIGATION_REASON_CODES
        or tool_class not in _SOURCE_NAVIGATION_TOOL_CLASSES
    ):
        return None
    sanitized: dict[str, Any] = {
        "policy_version": policy_version,
        "reason_code": reason_code,
        "tool_class": tool_class,
    }
    for key in (
        "missing_slot_count",
        "proposed_slot_count",
        "proposed_missing_slot_count",
        "discovery_count_since_read",
        "productive_discovery_count",
        "nonprogress_discovery_streak",
        "unique_candidate_count",
        "remaining_call_count",
        "coverage_budget_threshold",
    ):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            sanitized[key] = min(item, 10_000)
    if isinstance(value.get("has_actionable_candidates"), bool):
        sanitized["has_actionable_candidates"] = value["has_actionable_candidates"]
    if isinstance(value.get("coverage_budget_at_risk"), bool):
        sanitized["coverage_budget_at_risk"] = value["coverage_budget_at_risk"]
    return sanitized


def is_workspace_source_chain_goal(user_goal: object) -> bool:
    """只对高置信度的源码端到端/多层取证自然语言启用覆盖契约。"""
    return (
        isinstance(user_goal, str)
        and bool(_SOURCE_CHAIN_CODE_RE.search(user_goal))
        and bool(_SOURCE_CHAIN_RELATION_RE.search(user_goal))
    )


def build_workspace_source_chain_coverage(
    user_goal: object,
    observations: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """从用户点名端点和成功源码正文计算有界边证据状态。

    路径只能确定候选 owner，不能独自证明调用边。端点必须同时满足固定路径 taxonomy
    与该端点的直接调用信号；传输阶段必须同时具有 producer 与 consumer 信号。
    文件路径与正文仍是不可信数据，反馈和 metadata 只暴露计数与固定类别。
    """
    if not is_workspace_source_chain_goal(user_goal):
        return None
    assert isinstance(user_goal, str)
    endpoint_positions = sorted(
        (match.start(), name)
        for name, pattern in _ENDPOINT_GOAL_PATTERNS.items()
        if (match := pattern.search(user_goal)) is not None
    )
    required_endpoints = tuple(name for _, name in endpoint_positions)
    # 只有用户明确点名至少两个不同运行端时才建立硬覆盖门槛。普通单模块调用链
    # 仍由原有正文证据规则处理，避免把所有 code review 误判为跨进程链路。
    if len(required_endpoints) < 2:
        return None

    fragments = _successful_read_fragments(observations)
    paths = tuple(dict.fromkeys(str(item["path"]) for item in fragments))
    endpoint_evidence: set[str] = set()
    producer_evidence = False
    consumer_evidence = False
    for fragment in fragments:
        path = str(fragment["path"])
        evidence_text = str(fragment.get("evidence_text", ""))
        path_endpoints, _ = _classified_path_categories(path)
        for endpoint in required_endpoints:
            if (
                endpoint in path_endpoints
                and _ENDPOINT_DIRECT_EVIDENCE_PATTERNS[endpoint].search(evidence_text)
            ):
                endpoint_evidence.add(endpoint)
        producer_evidence = producer_evidence or bool(
            _TRANSPORT_PRODUCER_EVIDENCE_RE.search(evidence_text)
        )
        consumer_evidence = consumer_evidence or bool(
            _TRANSPORT_CONSUMER_EVIDENCE_RE.search(evidence_text)
        )
    covered_endpoints = tuple(
        name
        for name in required_endpoints
        if name in endpoint_evidence
    )
    required_stages = ("entry", "transport", "execution")
    covered_stage_set: set[str] = set()
    if required_endpoints[0] in endpoint_evidence:
        covered_stage_set.add("entry")
    if producer_evidence and consumer_evidence:
        covered_stage_set.add("transport")
    if required_endpoints[-1] in endpoint_evidence:
        covered_stage_set.add("execution")
    covered_stages = tuple(
        stage for stage in required_stages if stage in covered_stage_set
    )
    missing_endpoints = tuple(
        name for name in required_endpoints if name not in covered_endpoints
    )
    missing_stages = tuple(stage for stage in required_stages if stage not in covered_stages)
    endpoint_slot_order: list[str] = []
    if required_endpoints:
        endpoint_slot_order.append(f"endpoint:{required_endpoints[0]}")
    if len(required_endpoints) > 1:
        endpoint_slot_order.append(f"endpoint:{required_endpoints[-1]}")
    endpoint_slot_order.extend(
        f"endpoint:{endpoint}" for endpoint in required_endpoints[1:-1]
    )
    required_evidence_slots = tuple(
        dict.fromkeys(
            (*endpoint_slot_order, "transport:producer", "transport:consumer")
        )
    )
    covered_evidence_slots = {
        *(f"endpoint:{endpoint}" for endpoint in endpoint_evidence),
        *(("transport:producer",) if producer_evidence else ()),
        *(("transport:consumer",) if consumer_evidence else ()),
    }
    missing_evidence_slots = tuple(
        slot for slot in required_evidence_slots if slot not in covered_evidence_slots
    )
    return {
        "schema": "workspace-source-chain-coverage-v3",
        "required_endpoint_count": len(required_endpoints),
        "covered_endpoint_count": len(covered_endpoints),
        "missing_endpoints": missing_endpoints,
        "required_stage_count": len(required_stages),
        "covered_stage_count": len(covered_stages),
        "missing_stages": missing_stages,
        "required_evidence_slot_count": len(required_evidence_slots),
        "covered_evidence_slot_count": len(covered_evidence_slots),
        "missing_evidence_slots": missing_evidence_slots,
        "unique_source_paths": len(paths),
        "complete": not missing_evidence_slots,
    }


def workspace_source_chain_requires_more_evidence(
    user_goal: object,
    observations: Iterable[dict[str, Any]],
) -> bool:
    coverage = build_workspace_source_chain_coverage(user_goal, observations)
    return coverage is not None and coverage["complete"] is not True


def _is_source_path(value: object) -> bool:
    return isinstance(value, str) and PurePosixPath(value).suffix.casefold() in _SOURCE_SUFFIXES


def _bounded_excerpt(content: object) -> str:
    if not isinstance(content, str) or not content:
        return ""
    if len(content) <= _MAX_LEDGER_EXCERPT_CHARS:
        return content
    marker = "\n…[Runtime ledger excerpt omitted]…\n"
    segment = (_MAX_LEDGER_EXCERPT_CHARS - 2 * len(marker)) // 3
    middle = max(0, len(content) // 2 - segment // 2)
    return (
        content[:segment]
        + marker
        + content[middle : middle + segment]
        + marker
        + content[-segment:]
    )


def _successful_read_fragments(
    observations: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("ok") is not True:
            continue
        tool_name = observation.get("tool_name")
        data = observation.get("data")
        if not isinstance(data, dict):
            continue
        items: list[dict[str, Any]] = []
        if tool_name == "workspace.read_file":
            items = [data]
        elif tool_name == "workspace.read_files" and isinstance(data.get("files"), list):
            items = [
                item
                for item in data["files"]
                if isinstance(item, dict) and item.get("ok") is True
            ]
        for item in items:
            path = item.get("path")
            if not _is_source_path(path):
                continue
            fragments.append(
                {
                    "path": path,
                    "start_line": item.get("start_line", 1),
                    "end_line": item.get("end_line"),
                    "total_lines": item.get("total_lines"),
                    "excerpt": _bounded_excerpt(item.get("content")),
                    # 只在本地确定性 coverage 中使用，不进入 Context ledger 或 Runtime feedback。
                    "evidence_text": item.get("content")
                    if isinstance(item.get("content"), str)
                    else "",
                }
            )
    return fragments


def build_workspace_source_evidence_ledger(
    observations: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """压缩源码正文观测，作为不可信 data message 跨越上下文后缀裁剪。

    首尾各保留一组不同路径，避免长调用链只剩中间层或只剩终点。路径和正文
    永远不会进入 system feedback；Prompt injection 边界仍与原 ToolResult 相同。
    """
    fragments = _successful_read_fragments(observations)
    if not fragments:
        return None
    ordered_paths = list(dict.fromkeys(str(item["path"]) for item in fragments))
    if len(ordered_paths) > _MAX_LEDGER_PATHS:
        head = _MAX_LEDGER_PATHS // 2
        selected_paths = [*ordered_paths[:head], *ordered_paths[-(_MAX_LEDGER_PATHS - head) :]]
    else:
        selected_paths = ordered_paths
    selected = set(selected_paths)
    fragments_by_path: dict[str, list[dict[str, Any]]] = {}
    read_counts: dict[str, int] = {}
    for fragment in fragments:
        path = str(fragment["path"])
        read_counts[path] = read_counts.get(path, 0) + 1
        if path in selected:
            fragments_by_path.setdefault(path, []).append(fragment)
    entries: list[dict[str, Any]] = []
    for path in selected_paths:
        path_fragments = fragments_by_path.get(path, [])
        if not path_fragments:
            continue
        retained = [path_fragments[0]]
        if len(path_fragments) > 1:
            retained.append(path_fragments[-1])
        entries.append(
            {
                "path": path,
                "read_count": read_counts[path],
                "fragments": [
                    {
                        key: value
                        for key, value in fragment.items()
                        if key not in {"path", "evidence_text"}
                    }
                    for fragment in retained
                ],
            }
        )
    return {
        "schema": "workspace-source-evidence-ledger-v1",
        "source_reads": len(fragments),
        "unique_source_paths": len(ordered_paths),
        "omitted_source_paths": max(0, len(ordered_paths) - len(entries)),
        "repeated_source_paths": sum(1 for count in read_counts.values() if count > 1),
        "entries": entries,
    }


def build_workspace_source_chain_feedback(
    observations: Iterable[dict[str, Any]],
    *,
    user_goal: object = "",
    remaining_calls: int | None = None,
    slot_attempts: dict[str, int] | None = None,
) -> str | None:
    """根据真实源码读取生成独立、短小且不会被导航反馈挤掉的链路约束。"""
    del slot_attempts  # 旧 checkpoint/调用方兼容；v5 导航不再选择唯一活动槽。
    observation_items = tuple(observations)
    ledger = build_workspace_source_evidence_ledger(observation_items)
    if ledger is None:
        return None
    coverage = build_workspace_source_chain_coverage(user_goal, observation_items)
    feedback = (
        "源码证据覆盖：已读取 "
        f"{ledger['unique_source_paths']} 个不同源码文件，共 {ledger['source_reads']} 个片段；"
        f"重复读取路径 {ledger['repeated_source_paths']} 个。调用链必须逐边核对 "
        "caller/producer → transport → consumer/dispatcher → executor；接口、DTO、adapter、helper、"
        "service、文件路径或方法定义不等于调用边。入口端必须读到向下一层发起请求/调用的正文，传输层"
        "必须同时读到 producer 与 consumer，执行端必须读到外层循环实际调用 runner/executor 的正文。"
        "先补用户指定终点的外层循环/dispatch 直接证据，再加深已读层；"
        "证据账本保留了首尾源码片段，避免因上下文裁剪而重复读取。"
    )
    if coverage is not None:
        feedback += (
            " 跨层覆盖计划：用户明确点名的运行端已覆盖 "
            f"{coverage['covered_endpoint_count']}/{coverage['required_endpoint_count']}，"
            "入口/传输/执行阶段已覆盖 "
            f"{coverage['covered_stage_count']}/{coverage['required_stage_count']}。"
        )
        if coverage["complete"] is not True:
            feedback += (
                " 当前尚缺必需证据类别："
                f"{workspace_source_chain_missing_summary(coverage)}。"
                f"共 {len(coverage['missing_evidence_slots'])} 个未覆盖证据面。"
                "可以按任意顺序搜索或读取任一缺口，也可以读取跨多个缺口的批量候选；已有候选时优先"
                "读取正文。Runtime 会退回完全重复的成功动作或连续没有新增候选的发现循环；覆盖预算"
                "保护窗口开启后，还会退回不指向任一缺口的发现动作，但不会指定唯一调查槽。在覆盖闭合"
                "前不得 finish。"
            )
    normalized_remaining = (
        remaining_calls
        if isinstance(remaining_calls, int)
        and not isinstance(remaining_calls, bool)
        and remaining_calls > 0
        else None
    )
    if coverage is not None and coverage["complete"] is not True:
        missing_count = len(coverage["missing_evidence_slots"])
        protection_threshold = missing_count * 2
        if (
            normalized_remaining is not None
            and normalized_remaining <= protection_threshold
        ):
            feedback += (
                f" 覆盖预算保护窗口已开启：剩余 {normalized_remaining} 次工具调用，"
                f"仍有 {missing_count} 个证据面未覆盖。后续动作只能推进任一未覆盖证据面，"
                "顺序不受限制，也可以用一次批量读取同时补多个缺口；不要继续加深只属于已覆盖"
                "证据面的组件。覆盖闭合前不得 finish，不能用相邻实现补链。"
            )
    elif normalized_remaining is not None and normalized_remaining <= 4:
        feedback += (
            f" 当前只剩 {normalized_remaining} 次工具调用：若终点仍未闭合，下一次必须用于终点"
            "调用点；否则 finish 并明确未确认，不能用相邻实现补链。"
        )
    return feedback


def build_workspace_evidence_navigation_feedback(
    observations: Iterable[dict[str, Any]],
    *,
    remaining_calls: int | None = None,
) -> str | None:
    """根据真实 ToolResult 生成不含外部路径文本的取证阶段反馈。

    路径仅用于内存中的集合比较，绝不提升到 system 文本；模型从原始有界
    ToolResult 获取具体候选。这里仅提供确定性的阶段和数量约束。
    """
    searched_paths: set[str] = set()
    read_paths: set[str] = set()
    successful_searches = 0
    successful_reads = 0
    failed_batch_items = 0
    suggested_failure_paths = 0
    latest_path_failure_index = -1
    latest_path_discovery_index = -1

    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            continue
        tool_name = observation.get("tool_name")
        data = observation.get("data")
        if not isinstance(data, dict):
            continue
        if (
            tool_name in {"workspace.search_text", "workspace.search_files"}
            and observation.get("ok") is True
        ):
            latest_path_discovery_index = index
            if tool_name == "workspace.search_text":
                successful_searches += 1
            matches = data.get("matches")
            if isinstance(matches, list):
                for match in matches:
                    if len(searched_paths) >= _MAX_TRACKED_PATHS:
                        break
                    if isinstance(match, dict):
                        path = match.get("path")
                        if isinstance(path, str) and path:
                            searched_paths.add(path)
        elif tool_name == "workspace.read_file":
            if observation.get("ok") is True:
                path = data.get("path")
                if isinstance(path, str) and path:
                    read_paths.add(path)
                    successful_reads += 1
            else:
                latest_path_failure_index = index
                suggestions = data.get("suggested_paths")
                if isinstance(suggestions, list):
                    suggested_failure_paths += sum(
                        1 for value in suggestions if isinstance(value, str) and value
                    )
        elif tool_name == "workspace.list_files" and observation.get("ok") is not True:
            latest_path_failure_index = index
            suggestions = data.get("suggested_paths")
            if isinstance(suggestions, list):
                suggested_failure_paths += sum(
                    1 for value in suggestions if isinstance(value, str) and value
                )
        elif tool_name == "workspace.read_files":
            files = data.get("files")
            current_failed_items = 0
            if isinstance(files, list):
                for item in files:
                    if not isinstance(item, dict):
                        continue
                    if item.get("ok") is True:
                        path = item.get("path")
                        if isinstance(path, str) and path:
                            read_paths.add(path)
                            successful_reads += 1
                    else:
                        current_failed_items += 1
                        suggestions = item.get("suggested_paths")
                        if isinstance(suggestions, list):
                            suggested_failure_paths += sum(
                                1
                                for value in suggestions
                                if isinstance(value, str) and value
                            )
            if current_failed_items:
                failed_batch_items += current_failed_items
                latest_path_failure_index = index

    if successful_searches == 0 and latest_path_failure_index < 0:
        return None
    feedback: str | None = None
    if latest_path_failure_index > latest_path_discovery_index:
        suggestion_guidance = (
            f"最近失败结果提供了 {suggested_failure_paths} 个有界已存在候选；先从 suggested_paths 中"
            "选择与目标证据面一致的精确路径并读取，不得自行改写候选。"
            if suggested_failure_paths
            else "失败结果没有可信候选；先用 workspace.search_files 按文件名定位一次，再复制返回路径读取。"
        )
        failure_summary = (
            f"累计有 {failed_batch_items} 个批量条目失败（路径或范围无效）。"
            if failed_batch_items
            else "最近一次 Workspace 路径读取失败。"
        )
        feedback = (
            f"Workspace 取证纠错阶段：{failure_summary}下一步不得继续猜测路径。"
            f"{suggestion_guidance}"
        )
    else:
        unread_candidates = len(searched_paths - read_paths)
        if unread_candidates >= 2:
            feedback = (
                f"Workspace 取证阶段：正文搜索已定位 {unread_candidates} 个尚未读取的候选文件。"
                "先按用户要求的证据面筛选 2–6 个权威源码候选，并使用 workspace.read_files 合并读取；"
                "path 必须原样复制 ToolResult 的精确相对路径，不得凭记忆重构；搜索结果有行号时读取命中"
                "行附近范围。完成这一步前不要再次做无范围的宽泛搜索。"
            )
        elif unread_candidates == 1:
            feedback = (
                "Workspace 取证阶段：当前还有 1 个已定位候选未读取。优先使用 read_file 的 "
                "start_line/max_lines 获取命中附近直接证据，再判断是否存在明确证据缺口。"
            )
        elif successful_reads > 0:
            suffix = (
                f"其中 {failed_batch_items} 个批量条目失败，应只修正对应路径或范围。"
                if failed_batch_items
                else ""
            )
            feedback = (
                f"Workspace 取证阶段：已获得 {successful_reads} 个文件片段，当前搜索候选已读取。"
                "请逐项检查用户要求的证据面；证据已闭合则 finish，只有明确缺口时才用更具体的新关键词"
                f"进行有路径约束的补搜。{suffix}"
            )

    if feedback is None:
        return None
    if (
        isinstance(remaining_calls, int)
        and not isinstance(remaining_calls, bool)
        and 0 < remaining_calls <= 4
        and successful_reads > 0
    ):
        feedback += (
            f" 取证预算进入收口窗口（剩余 {remaining_calls} 次）：禁止重复读取已成功路径来继续加深同一"
            "证据面；下一次搜索/批量读取必须优先覆盖尚未取证的目标部分。多段或端到端问题只有在每个"
            "部分都有直接正文证据时才能作为完整答案，否则必须明确列出未确认部分。"
        )
    return feedback
