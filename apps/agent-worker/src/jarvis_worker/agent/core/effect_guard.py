"""Agent finish 的确定性工具证据校验。

本模块只识别高置信度的“明确调用某个已注册工具”指令，以及带有明确目标路径的
Workspace 文件创建命令。它不尝试用关键词理解所有自然语言意图，也不替代模型规划；
目标是阻止模型在明确工具任务中未执行工具就直接宣称完成。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

from jarvis_worker.agent.intents.rules import is_explicit_workspace_content_search_goal
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest

MAX_EFFECT_GUARD_GOAL_CHARS = 10_000
MAX_EFFECT_GUARD_TOOL_NAME_CHARS = 200
MAX_REQUIRED_TOOL_EVIDENCE = 8

WORKSPACE_CONTENT_EVIDENCE_TOOLS = frozenset(
    {"workspace.read_file", "workspace.read_files"}
)
WORKSPACE_CONTENT_SEARCH_EVIDENCE_TOOLS = frozenset({"workspace.search_text"})
WORKSPACE_METADATA_EVIDENCE_TOOLS = frozenset(
    {"workspace.list_files", "workspace.get_file_info"}
)
WORKSPACE_WRITE_EFFECT_TOOLS = frozenset(
    {"workspace.create_file", "workspace.create_directory", "workspace.move_path"}
)
WORKSPACE_DESTRUCTIVE_EFFECT_TOOLS = frozenset({"workspace.delete_path"})
WORKSPACE_EFFECT_TOOLS = WORKSPACE_WRITE_EFFECT_TOOLS | WORKSPACE_DESTRUCTIVE_EFFECT_TOOLS

_WORKSPACE_COLLECTION_STRONG_RE = re.compile(
    r"(?:相关(?:材料|资料|文件|文档)|(?:多份|多个|多篇|一组)(?:材料|资料|文件|文档)|"
    r"所有.{0,20}(?:材料|资料|文件|文档)|"
    r"\brelated\s+(?:materials?|documents?|files?)\b|"
    r"\bmultiple\s+(?:documents?|files?|sources?)\b)",
    re.IGNORECASE | re.DOTALL,
)
_WORKSPACE_COLLECTION_RELATION_RE = re.compile(
    r"(?:每一步|全过程|完整流程|从.{0,80}到|先.{0,80}再|核对|对照|比较|一致|冲突|"
    r"时间线|演变|\bworkflow\b|\bprocess\b|\bevery\s+step\b|"
    r"\bcross[- ]?check\b|\bcompare\b|\bconsisten(?:t|cy)\b|\bconflict\b)",
    re.IGNORECASE | re.DOTALL,
)
_WORKSPACE_COLLECTION_EVIDENCE_RE = re.compile(
    r"(?:依据|证据|材料|资料|文件|文档|记录|政策|规范|报告|"
    r"\bevidence\b|\bsources?\b|\bdocuments?\b|\bfiles?\b|\brecords?\b|\bpolic(?:y|ies)\b)",
    re.IGNORECASE,
)
_EXPLICIT_WORKSPACE_FILE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,16}"
    r"(?![A-Za-z0-9_.-])"
)
_CONDITIONAL_EXISTENCE_RE = re.compile(
    r"(?:\b(?:if|when)\b.{0,80}\b(?:already\s+)?exists?\b|"
    r"(?:如果|若|如).{0,80}(?:已经|已)?存在)",
    re.IGNORECASE | re.DOTALL,
)
_NO_OVERWRITE_RE = re.compile(
    r"(?:不要|请勿|禁止|不得|不能|不允许).{0,16}(?:覆盖|改写)|"
    r"\b(?:do\s+not|don't|never)\s+overwrite\b",
    re.IGNORECASE | re.DOTALL,
)
_EXCLUSIVE_SINGLE_WORKSPACE_EFFECT_RE = re.compile(
    r"(?:其他(?:文件|目录|路径|内容|项目)?(?:都|均|保持)?不(?:要)?(?:动|修改|移动|删除|处理)|"
    r"(?:不要|请勿|不得)动其他(?:文件|目录|路径|内容|项目)?|"
    r"\bleave\s+(?:all\s+)?other\s+(?:files?|directories|paths?|content|items?)\s+unchanged\b|"
    r"\bdo\s+not\s+(?:touch|modify|move|delete)\s+(?:any\s+)?other\b)",
    re.IGNORECASE,
)
_WORKSPACE_PATH_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?:[^\s，,。！？!?；;：:/\\`'\"“”]+/)*"
    r"[^\s，,。！？!?；;：:/\\`'\"“”]+\.[A-Za-z0-9]{1,16}"
    r"(?![A-Za-z0-9_.-])"
)

_CLAUSE_BOUNDARY = r"(?:^|[，,。！？!?；;：:\n])"
_ZH_COMMAND_PREFIX = r"(?:请(?:帮我)?|帮我|麻烦(?:你)?|务必|必须)"
_ZH_WORKSPACE_LOCATION = r"(?:(?:在|于)\s*(?:当前)?(?:工作区|workspace)(?:根目录)?(?:内|中)?\s*)?"
_FILE_PATH = r"[`'\"“”]?[^\s，,。！？!?；;：:]{1,300}\.[A-Za-z0-9]{1,16}[`'\"“”]?"
_WORKSPACE_CREATE_FILE_PATTERNS = (
    re.compile(
        _CLAUSE_BOUNDARY
        + r"\s*"
        + _ZH_COMMAND_PREFIX
        + r"\s*"
        + _ZH_WORKSPACE_LOCATION
        + r"(?:创建|新建|写入|保存(?:为)?)\s*(?:一个|该|这个)?\s*(?:文件\s*)?"
        + _FILE_PATH,
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^\s*"
        + _ZH_WORKSPACE_LOCATION
        + r"(?:创建|新建|写入|保存(?:为)?)\s*(?:一个|该|这个)?\s*(?:文件\s*)?"
        + _FILE_PATH,
        flags=re.IGNORECASE,
    ),
    re.compile(
        _CLAUSE_BOUNDARY
        + r"\s*(?:please\s+|must\s+)"
        + r"(?:create|write|save)\s+(?:the\s+)?(?:file\s+)?"
        + _FILE_PATH,
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:create|write|save)\s+(?:the\s+)?(?:file\s+)?" + _FILE_PATH,
        flags=re.IGNORECASE,
    ),
)


def find_required_goal_tools(
    user_goal: str,
    manifests: Iterable[ToolManifest],
) -> tuple[str, ...]:
    """返回 finish 前必须具有成功 ToolResult 的已注册工具。

    显式工具名指令保持原有规则。对于 Workspace 文件创建，只在命令式表达同时
    携带明确文件路径时建立要求，避免把“如何创建文件”等说明性问题误判为副作用。
    """
    manifest_items = tuple(manifests)
    required = list(find_explicitly_requested_tools(user_goal, manifest_items))
    enabled_names = {
        manifest.name
        for manifest in manifest_items
        if manifest.enabled and isinstance(manifest.name, str) and manifest.name
    }
    if (
        "workspace.create_file" in enabled_names
        and _has_workspace_create_file_directive(user_goal)
        and "workspace.create_file" not in required
        and len(required) < MAX_REQUIRED_TOOL_EVIDENCE
    ):
        required.append("workspace.create_file")
    return tuple(required)


def find_explicitly_requested_tools(
    user_goal: str,
    manifests: Iterable[ToolManifest],
) -> tuple[str, ...]:
    """返回用户以高置信度明确要求调用的 enabled 工具名。

    只接受命令式表达，例如“请只调用 workspace.create_file”或
    “please use workspace.create_file”。“如何使用 workspace.create_file”这类
    说明性问题不会被识别为执行要求。
    """
    if not isinstance(user_goal, str) or not user_goal.strip():
        return ()
    goal = user_goal[:MAX_EFFECT_GUARD_GOAL_CHARS]

    required: list[str] = []
    for manifest in manifests:
        if not manifest.enabled or not manifest.name:
            continue
        if len(manifest.name) > MAX_EFFECT_GUARD_TOOL_NAME_CHARS:
            continue
        if _has_explicit_tool_directive(goal, manifest.name):
            required.append(manifest.name)
            if len(required) >= MAX_REQUIRED_TOOL_EVIDENCE:
                break
    return tuple(required)


def find_missing_successful_tool_evidence(
    required_tool_names: Iterable[str],
    observations: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    """返回当前 Run 尚无成功 ToolResult observation 的必需工具。"""
    successful = {
        observation.get("tool_name")
        for observation in observations
        if isinstance(observation, dict) and observation.get("ok") is True
    }
    return tuple(name for name in required_tool_names if name not in successful)


def conditional_no_overwrite_target(user_goal: str) -> str:
    """Return one safe relative target for an explicit create-if-absent request.

    The result is intentionally narrower than general path extraction.  A completion
    alternative is granted only when the same goal contains an existence condition,
    an explicit no-overwrite instruction, and exactly one normalized file target.
    This function never creates ToolRequest arguments or grants a capability.
    """
    if not isinstance(user_goal, str) or not user_goal.strip():
        return ""
    goal = user_goal[:MAX_EFFECT_GUARD_GOAL_CHARS]
    if not (
        _has_workspace_create_file_directive(goal)
        and _CONDITIONAL_EXISTENCE_RE.search(goal)
        and _NO_OVERWRITE_RE.search(goal)
    ):
        return ""
    candidates: list[str] = []
    for raw in _WORKSPACE_PATH_CANDIDATE_RE.findall(goal):
        normalized = _normalize_relative_workspace_target(raw)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates[0] if len(candidates) == 1 else ""


def has_exclusive_single_workspace_effect_scope(user_goal: str) -> bool:
    """Return whether the goal safely closes the effect scope after one target.

    This is intentionally conservative: the goal must explicitly forbid touching
    other workspace items and contain exactly one normalized file target.  It does
    not infer ToolRequest arguments or decide which effect tool to use.
    """
    if not isinstance(user_goal, str) or not user_goal.strip():
        return False
    goal = user_goal[:MAX_EFFECT_GUARD_GOAL_CHARS]
    if _EXCLUSIVE_SINGLE_WORKSPACE_EFFECT_RE.search(goal) is None:
        return False
    candidates = {
        normalized
        for raw in _WORKSPACE_PATH_CANDIDATE_RE.findall(goal)
        if (normalized := _normalize_relative_workspace_target(raw))
    }
    return len(candidates) == 1


def has_confirmed_workspace_target(
    observations: Iterable[dict[str, Any]],
    target: str,
) -> bool:
    """Check trusted ToolResult observations for exact target-existence evidence.

    Positive, exact-path evidence from read/discovery tools is accepted.  A
    ``PATH_ALREADY_EXISTS`` result from the non-overwriting create tool is also
    terminal truth.  Fuzzy names, summaries, model prose, and failed discovery calls
    never satisfy the precondition.
    """
    normalized_target = _normalize_relative_workspace_target(target)
    if not normalized_target:
        return False
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        tool_name = observation.get("tool_name")
        action = observation.get("model_action")
        arguments = action.get("arguments") if isinstance(action, dict) else {}
        if not isinstance(arguments, dict):
            arguments = {}
        if observation.get("ok") is False and tool_name == "workspace.create_file":
            error = observation.get("error")
            if (
                isinstance(error, dict)
                and error.get("code") == "PATH_ALREADY_EXISTS"
                and _same_workspace_target(arguments.get("path"), normalized_target)
            ):
                return True
            continue
        if observation.get("ok") is not True:
            continue
        data = observation.get("data")
        if not isinstance(data, dict):
            continue
        if tool_name in {"workspace.get_file_info", "workspace.read_file"}:
            if _same_workspace_target(data.get("path"), normalized_target):
                return True
        elif tool_name == "workspace.read_files":
            files = data.get("files")
            if isinstance(files, list) and any(
                isinstance(item, dict)
                and item.get("ok") is True
                and _same_workspace_target(item.get("path"), normalized_target)
                for item in files
            ):
                return True
        elif tool_name == "workspace.search_files":
            matches = data.get("matches")
            if isinstance(matches, list) and any(
                isinstance(item, dict)
                and _same_workspace_target(item.get("path"), normalized_target)
                for item in matches
            ):
                return True
        elif tool_name == "workspace.list_files":
            search_path = _normalize_relative_workspace_target(
                arguments.get("path"),
                allow_directory=True,
            )
            entries = data.get("entries")
            if isinstance(entries, list) and any(
                isinstance(item, dict)
                and _same_workspace_target(
                    str(PurePosixPath(search_path) / str(item.get("name", "")))
                    if search_path
                    else item.get("name"),
                    normalized_target,
                )
                for item in entries
            ):
                return True
    return False


def find_latest_failed_required_tool(
    required_tool_names: Iterable[str],
    observations: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """返回最近一次已执行但失败的必需工具证据。

    EffectGuard 必须区分“没有调用”和“调用后失败”。后者的真实 ToolResult
    才是终态 owner，不能被改写成 REQUIRED_TOOL_NOT_EXECUTED。
    """
    required = frozenset(required_tool_names)
    for observation in reversed(tuple(observations)):
        if (
            isinstance(observation, dict)
            and observation.get("tool_name") in required
            and observation.get("ok") is False
            and isinstance(observation.get("error"), dict)
        ):
            return observation
    return None


def build_effect_guard_feedback(missing_tool_names: Iterable[str]) -> str:
    """构造给下一轮模型的有界、可信 Runtime 反馈。"""
    names = tuple(missing_tool_names)
    joined = ", ".join(names)
    feedback = (
        "上一次 finish 已被 Runtime 拒绝：当前任务的用户要求或运行策略要求调用工具 "
        f"{joined}，但当前 Run 尚未观察到这些工具的成功 ToolResult。"
        "你必须先调用缺失的工具，并根据真实工具结果继续决策；不得直接声称操作成功。"
    )
    if any("workspace 多材料正文覆盖" in name for name in names):
        feedback += (
            "当前缺口是材料覆盖而非答案措辞：下一步必须从已读锚点扩展到相关父目录，使用"
            "目录枚举、非零命中的更广文件发现或正文搜索，并读取发现的每个相关文件；只有"
            "有界发现确认没有其他文件时，才可按单一来源诚实收口。仅切换 "
            "search_files/search_text、改变或缩小 path 但保持相同 query，或重复读取同一文件，"
            "都不算完整范围发现；零命中的文件名搜索也不能证明没有关联正文。优先从已读正文"
            "中的流程阶段、角色、引用或关系词扩展发现范围。"
        )
    return feedback


def find_required_workspace_effect_mismatch(
    required_tool_names: Iterable[str],
    proposed_tool_name: str,
) -> str | None:
    """Return the one required Workspace effect contradicted by a proposal.

    Read-only discovery is allowed before an effect. A proposal is contradictory only
    when it selects a *different* Workspace side-effect tool from the one unambiguously
    required by the host-owned goal contract. Multiple required effects are left to the
    planner because their ordering cannot be inferred safely here.
    """
    required_effects = tuple(
        dict.fromkeys(name for name in required_tool_names if name in WORKSPACE_EFFECT_TOOLS)
    )
    if (
        proposed_tool_name not in WORKSPACE_EFFECT_TOOLS
        or len(required_effects) != 1
        or proposed_tool_name == required_effects[0]
    ):
        return None
    return required_effects[0]


def build_workspace_effect_mismatch_feedback(
    *,
    expected_tool_name: str,
    proposed_tool_name: str,
) -> str:
    """Build bounded trusted feedback without echoing paths or file content."""
    return (
        "上一次工具动作已被 Runtime 在授权前拒绝：用户目标明确要求的 Workspace "
        f"副作用是 {expected_tool_name}，但候选动作选择了 {proposed_tool_name}。"
        f"下一步必须使用 {expected_tool_name} 并根据原始目标构造参数；不得用其他写入或"
        "删除工具替代，也不得声称动作已完成。"
    )


def retrieval_mode(intent: dict[str, Any] | None) -> str:
    """从可恢复状态安全读取检索模式；损坏值按 skip 处理。"""
    if not isinstance(intent, dict):
        return "skip"
    retrieval = intent.get("retrieval")
    if not isinstance(retrieval, dict):
        return "skip"
    mode = retrieval.get("mode")
    return mode if mode in {"skip", "retrieve", "required"} else "skip"


def requires_rag_search(intent: dict[str, Any] | None) -> bool:
    mode = retrieval_mode(intent)
    workspace_evidence, workspace_action, _ = workspace_semantics(intent)
    # retrieve 只表示“可能获益”，不能覆盖一个已经确定由本地 Workspace
    # 文件取证回答的任务。用户明确依赖已保存资料时 Intent 必须使用 required，
    # 此时允许 RAG 与 Workspace 两条证据链同时存在。
    if (
        mode == "retrieve"
        and workspace_action == "read"
        and workspace_evidence in {"metadata", "required"}
    ):
        return False
    return mode in {"retrieve", "required"} and rag_document_scope(intent) != "unresolved"


def rag_document_scope(intent: dict[str, Any] | None) -> str:
    if not isinstance(intent, dict):
        return "none"
    retrieval = intent.get("retrieval")
    if not isinstance(retrieval, dict):
        return "none"
    scope = retrieval.get("document_scope")
    return scope if scope in {"none", "all", "selected", "unresolved"} else "none"


def resolved_rag_document_ids(intent: dict[str, Any] | None) -> tuple[str, ...]:
    if rag_document_scope(intent) != "selected" or not isinstance(intent, dict):
        return ()
    retrieval = intent.get("retrieval")
    values = retrieval.get("resolved_document_ids") if isinstance(retrieval, dict) else None
    if not isinstance(values, list) or len(values) > 20:
        return ()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value in result:
            return ()
        result.append(value)
    return tuple(result)


def required_effect_tools(
    intent: dict[str, Any] | None,
    available_tool_names: frozenset[str],
) -> tuple[str, ...]:
    """Map validated effect semantics to platform tools; LLM never supplies names."""
    if not isinstance(intent, dict) or not isinstance(intent.get("effects"), dict):
        return ()
    effects = intent["effects"]
    mapping = (
        ("knowledge_write", "knowledge.create_document"),
        ("rag_ingestion", "rag.ingest_artifact"),
    )
    return tuple(
        tool_name
        for effect_name, tool_name in mapping
        if effects.get(effect_name) == "required" and tool_name in available_tool_names
    )


def workspace_semantics(intent: dict[str, Any] | None) -> tuple[str, str, str]:
    """从已校验 Intent 安全读取 Workspace evidence/action/ambiguity。"""
    if not isinstance(intent, dict) or not isinstance(intent.get("workspace"), dict):
        return "skip", "none", "clear"
    workspace = intent["workspace"]
    evidence = workspace.get("evidence")
    action = workspace.get("action")
    ambiguity = workspace.get("ambiguity")
    if evidence not in {"skip", "metadata", "required"}:
        evidence = "skip"
    if action not in {"none", "read", "write", "destructive"}:
        action = "none"
    if ambiguity not in {"clear", "clarification_required"}:
        ambiguity = "clear"
    return evidence, action, ambiguity


def find_missing_workspace_evidence(
    intent: dict[str, Any] | None,
    observations: Iterable[dict[str, Any]],
    *,
    user_goal: str = "",
    workspace_effect_satisfied: bool = False,
) -> tuple[str, ...]:
    """按结构化语义校验 Workspace ToolResult，并约束多材料任务的覆盖下限。

    单文件正文任务仍只要求一次成功读取。用户明确要求相关材料、逐步依据、流程核对或
    跨资料一致性时，精确命中一份文件只算锚点：至少需要两个不同正文来源；若工作区
    确实只有一份，则必须再完成一次不同范围的发现且没有留下未读候选，才允许诚实收口。
    这里不指定路径、查询词、业务文档类型或答案内容。
    """
    observation_items = tuple(observations)
    evidence, action, ambiguity = workspace_semantics(intent)
    successful = {
        item.get("tool_name")
        for item in observation_items
        if isinstance(item, dict) and item.get("ok") is True
    }
    missing: list[str] = []
    # 成功的 Workspace 写入结果本身就是可信元数据证据：create/move ToolResult
    # 会返回目标 path、size/hash 等落盘事实。不能在副作用已经完成后，仅因模型
    # Intent 把同一目标误标为 read+metadata，就再强迫一次 list/get_info 并把合理
    # 的 finish 锁进 tool-required。正文证据仍只接受 read/search 工具，未放宽。
    metadata_evidence_tools = WORKSPACE_METADATA_EVIDENCE_TOOLS | WORKSPACE_WRITE_EFFECT_TOOLS
    if evidence == "metadata" and not successful.intersection(metadata_evidence_tools):
        missing.append("workspace 目录/元数据读取（list_files/get_file_info）")
    has_content_evidence = bool(successful.intersection(WORKSPACE_CONTENT_EVIDENCE_TOOLS))
    has_scoped_search_evidence = bool(
        is_explicit_workspace_content_search_goal(user_goal)
        and successful.intersection(WORKSPACE_CONTENT_SEARCH_EVIDENCE_TOOLS)
    )
    if evidence == "required" and not (
        has_content_evidence or has_scoped_search_evidence
    ):
        missing.append("workspace 文件正文读取（read_file/read_files，或明确正文搜索任务的 search_text）")
    if (
        evidence == "required"
        and action == "read"
        and ambiguity == "clear"
        and _requires_workspace_collection_evidence(user_goal)
        and not _has_workspace_collection_coverage(observation_items)
    ):
        missing.append(
            "workspace 多材料正文覆盖（不同正文来源，或单一来源经二次独立发现确认）"
        )
    if (
        ambiguity == "clear"
        and action == "write"
        and not workspace_effect_satisfied
        and not successful.intersection(WORKSPACE_WRITE_EFFECT_TOOLS)
    ):
        missing.append("workspace 写入操作（create_file/create_directory/move_path）")
    if (
        ambiguity == "clear"
        and action == "destructive"
        and not workspace_effect_satisfied
        and not successful.intersection(WORKSPACE_DESTRUCTIVE_EFFECT_TOOLS)
    ):
        missing.append("workspace.delete_path")
    return tuple(missing)


def _requires_workspace_collection_evidence(user_goal: str) -> bool:
    if not isinstance(user_goal, str) or not user_goal.strip():
        return False
    goal = user_goal[:MAX_EFFECT_GUARD_GOAL_CHARS]
    if _WORKSPACE_COLLECTION_STRONG_RE.search(goal):
        return True
    # 用户明确只点名一个文件时保持单文件语义；“核对 report.md”不应被扩成全库研究。
    if len(_EXPLICIT_WORKSPACE_FILE_RE.findall(goal)) == 1:
        return False
    return bool(
        _WORKSPACE_COLLECTION_RELATION_RE.search(goal)
        and _WORKSPACE_COLLECTION_EVIDENCE_RE.search(goal)
    )


def _has_workspace_collection_coverage(
    observations: Iterable[dict[str, Any]],
) -> bool:
    read_paths = _successful_workspace_read_paths(observations)
    if not read_paths:
        return False
    discovery_fingerprints, discovered_paths = _workspace_discovery_progress(observations)
    if discovered_paths - read_paths:
        return False
    if len(read_paths) >= 2:
        return True
    return len(discovery_fingerprints) >= 2


def _successful_workspace_read_paths(
    observations: Iterable[dict[str, Any]],
) -> frozenset[str]:
    paths: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("ok") is not True:
            continue
        data = observation.get("data")
        if not isinstance(data, dict):
            continue
        if observation.get("tool_name") == "workspace.read_file":
            path = data.get("path")
            if isinstance(path, str) and path:
                paths.add(path)
        elif observation.get("tool_name") == "workspace.read_files":
            files = data.get("files")
            if not isinstance(files, list):
                continue
            for item in files:
                if not isinstance(item, dict) or item.get("ok") is not True:
                    continue
                path = item.get("path")
                if isinstance(path, str) and path:
                    paths.add(path)
    return frozenset(paths)


def _workspace_discovery_progress(
    observations: Iterable[dict[str, Any]],
) -> tuple[frozenset[tuple[str, str]], frozenset[str]]:
    # “独立发现”必须改变发现语义，而不是只改变工具或搜索起点。对于关键词搜索，
    # path 只限制扫描范围，并不会让同一精确 query 找到不含该词的关联材料；因此同一
    # query 即使换工具、改 path，也只能算一个锚点。list_files 是无关键词枚举，按目录
    # 分开记账；get_file_info 只检查已知路径，不具备发现其他材料的能力。
    fingerprints: set[tuple[str, str]] = set()
    query_fingerprints: set[str] = set()
    candidates: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict) or observation.get("ok") is not True:
            continue
        tool_name = observation.get("tool_name")
        if tool_name not in {
            "workspace.search_text",
            "workspace.search_files",
            "workspace.list_files",
        }:
            continue
        model_action = observation.get("model_action")
        arguments = model_action.get("arguments") if isinstance(model_action, dict) else {}
        if not isinstance(arguments, dict):
            arguments = {}
        path = arguments.get("path", ".")
        query = arguments.get("query", "")
        normalized_path = path.strip() if isinstance(path, str) else "."
        data = observation.get("data")
        if not isinstance(data, dict):
            continue
        values = (
            data.get("matches")
            if tool_name in {"workspace.search_text", "workspace.search_files"}
            else data.get("entries")
        )
        if not isinstance(values, list):
            continue
        if tool_name in {"workspace.search_text", "workspace.search_files"}:
            normalized_query = _normalize_discovery_query(query)
            if not normalized_query:
                continue
            # search_files 仅查文件名；零命中不能证明相关正文不存在。
            # search_text 即使零命中也提供了一个有界正文范围的否定证据。
            if (
                (tool_name == "workspace.search_text" or values)
                and not any(
                _queries_share_discovery_anchor(normalized_query, previous)
                for previous in query_fingerprints
                )
            ):
                query_fingerprints.add(normalized_query)
        else:
            fingerprints.add(("list", normalized_path or "."))
        for item in values:
            if not isinstance(item, dict):
                continue
            if tool_name == "workspace.list_files":
                if item.get("type") != "file":
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                candidate = (
                    str(PurePosixPath(normalized_path) / name)
                    if normalized_path not in {"", "."}
                    else name
                )
            else:
                if tool_name == "workspace.search_files" and item.get("type") != "file":
                    continue
                candidate = item.get("path")
            if isinstance(candidate, str) and candidate:
                candidates.add(candidate)
    fingerprints.update(("query", query) for query in query_fingerprints)
    return frozenset(fingerprints), frozenset(candidates)


def _normalize_discovery_query(value: object) -> str:
    """折叠不影响检索语义的大小写、全半角与空白差异。"""
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _queries_share_discovery_anchor(left: str, right: str) -> bool:
    """识别同一标识符的裁剪、扩写或仅追加限定词，避免伪装成语义扩展。"""
    if left == right or left in right or right in left:
        return True
    left_tokens = frozenset(left.split())
    right_tokens = frozenset(right.split())
    return bool(left_tokens and right_tokens) and (
        left_tokens.issubset(right_tokens) or right_tokens.issubset(left_tokens)
    )


def workspace_requires_clarification(intent: dict[str, Any] | None) -> bool:
    return workspace_semantics(intent)[2] == "clarification_required"


def intent_requires_clarification(intent: dict[str, Any] | None) -> bool:
    """普通未知目标只能进入无工具的 host-owned 澄清终点。"""
    return isinstance(intent, dict) and intent.get("primary_intent") == "unknown"


def find_latest_failed_workspace_evidence(
    intent: dict[str, Any] | None,
    observations: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    evidence, action, ambiguity = workspace_semantics(intent)
    relevant: set[str] = set()
    if evidence == "metadata":
        relevant.update(WORKSPACE_METADATA_EVIDENCE_TOOLS)
    if evidence == "required":
        relevant.update(WORKSPACE_CONTENT_EVIDENCE_TOOLS)
    if ambiguity == "clear" and action == "write":
        relevant.update(WORKSPACE_WRITE_EFFECT_TOOLS)
    if ambiguity == "clear" and action == "destructive":
        relevant.update(WORKSPACE_DESTRUCTIVE_EFFECT_TOOLS)
    for observation in reversed(tuple(observations)):
        if (
            isinstance(observation, dict)
            and observation.get("tool_name") in relevant
            and observation.get("ok") is False
            and isinstance(observation.get("error"), dict)
        ):
            return observation
    return None


def _has_explicit_tool_directive(user_goal: str, tool_name: str) -> bool:
    escaped_name = re.escape(tool_name)
    quoted_name = rf"[`'\"]?{escaped_name}[`'\"]?(?![A-Za-z0-9_.-])"
    patterns = (
        # 请调用 / 请只调用 / 必须使用 / 务必执行
        rf"(?:请\s*(?:只|仅)?|必须|务必)\s*(?:调用|使用|执行)\s*(?:工具\s*)?{quoted_name}",
        # 以动词或“只/仅”开头的直接命令
        rf"^\s*(?:只|仅)?\s*(?:调用|使用|执行)\s*(?:工具\s*)?{quoted_name}",
        # please call / please only use
        rf"please\s+(?:only\s+)?(?:call|use|execute|run)\s+"
        rf"(?:the\s+)?(?:tool\s+)?{quoted_name}",
        # 以英文动词、only 或 must 开头的直接命令
        rf"^\s*(?:only\s+|must\s+)?(?:call|use|execute|run)\s+"
        rf"(?:the\s+)?(?:tool\s+)?{quoted_name}",
    )
    return any(re.search(pattern, user_goal, flags=re.IGNORECASE) for pattern in patterns)


def _has_workspace_create_file_directive(user_goal: str) -> bool:
    if not isinstance(user_goal, str) or not user_goal.strip():
        return False
    goal = user_goal[:MAX_EFFECT_GUARD_GOAL_CHARS]
    return any(pattern.search(goal) for pattern in _WORKSPACE_CREATE_FILE_PATTERNS)


def _normalize_relative_workspace_target(
    value: object,
    *,
    allow_directory: bool = False,
) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip().strip("`'\"“”")
    if (
        not normalized
        or len(normalized) > 500
        or "\\" in normalized
        or "\x00" in normalized
        or normalized.startswith("/")
    ):
        return ""
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    if not allow_directory and not path.suffix:
        return ""
    return path.as_posix()


def _same_workspace_target(value: object, target: str) -> bool:
    return _normalize_relative_workspace_target(value) == target
