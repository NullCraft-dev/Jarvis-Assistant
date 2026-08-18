"""Intent Layer 的版本化结构化契约。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

RetrievalMode = Literal["skip", "retrieve", "required"]
EffectMode = Literal["skip", "optional", "required"]
DocumentScope = Literal["none", "all", "selected", "unresolved"]
WorkspaceEvidenceMode = Literal["skip", "metadata", "required"]
WorkspaceActionMode = Literal["none", "read", "write", "destructive"]
WorkspaceAmbiguityMode = Literal["clear", "clarification_required"]
WorkspaceListingEntryType = Literal["file", "dir", "symlink", "other"]
INTENT_POLICY_VERSION = "intent-llm-v7"
LEGACY_INTENT_POLICY_VERSIONS = frozenset({"intent-llm-v5", "intent-llm-v6"})
LEGACY_INTENT_POLICIES_WITHOUT_LISTING = frozenset({"intent-llm-v5"})
PRIMARY_INTENTS = frozenset(
    {
        "conversation",
        "task",
        "knowledge_question",
        "document_question",
        "research_task",
        "knowledge_write",
        "rag_ingestion",
        "unknown",
    }
)
RETRIEVAL_MODES = frozenset({"skip", "retrieve", "required"})
DOCUMENT_SCOPES = frozenset({"none", "all", "selected", "unresolved"})
EFFECT_MODES = frozenset({"skip", "optional", "required"})
WORKSPACE_EVIDENCE_MODES = frozenset({"skip", "metadata", "required"})
WORKSPACE_ACTION_MODES = frozenset({"none", "read", "write", "destructive"})
WORKSPACE_AMBIGUITY_MODES = frozenset({"clear", "clarification_required"})
WORKSPACE_LISTING_ENTRY_TYPES = frozenset({"file", "dir", "symlink", "other"})


@dataclass(frozen=True)
class IntentDocument:
    """Runtime 提供给 Intent LLM 的匿名、Workspace-scoped 文档选项。"""

    key: str
    document_id: str
    title: str
    created_at: str
    identity_excerpt: str = ""

    def to_prompt_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "title": self.title,
            "created_at": self.created_at,
            "identity_excerpt": self.identity_excerpt,
        }


@dataclass(frozen=True)
class IntentRuntimeContext:
    """一次 Intent 提取使用的冻结可信上下文。"""

    documents: tuple[IntentDocument, ...] = ()

    def to_state_dict(self) -> dict[str, Any]:
        return {"documents": [asdict(item) for item in self.documents]}

    @classmethod
    def from_state_dict(cls, value: object) -> "IntentRuntimeContext":
        if not isinstance(value, dict) or set(value) != {"documents"}:
            raise ValueError("intent runtime context 结构无效")
        documents = value.get("documents")
        if not isinstance(documents, list) or len(documents) > 50:
            raise ValueError("intent document catalog 无效")
        parsed: list[IntentDocument] = []
        seen_keys: set[str] = set()
        seen_ids: set[str] = set()
        for item in documents:
            if not isinstance(item, dict) or set(item) != {
                "key",
                "document_id",
                "title",
                "created_at",
                "identity_excerpt",
            }:
                raise ValueError("intent document catalog item 无效")
            if not all(isinstance(item[key], str) for key in item):
                raise ValueError("intent document catalog field 无效")
            document = IntentDocument(**item)
            if (
                re.fullmatch(r"doc_[1-9][0-9]?", document.key) is None
                or len(document.title) > 500
                or len(document.created_at) > 100
                or len(document.identity_excerpt) > 600
                or document.key in seen_keys
                or document.document_id in seen_ids
            ):
                raise ValueError("intent document catalog identity 无效")
            try:
                if str(UUID(document.document_id)) != document.document_id:
                    raise ValueError("intent document catalog ID 非 canonical UUID")
            except ValueError:
                raise ValueError("intent document catalog ID 无效") from None
            seen_keys.add(document.key)
            seen_ids.add(document.document_id)
            parsed.append(document)
        return cls(tuple(parsed))


@dataclass(frozen=True)
class IntentEffects:
    """个人知识库与 RAG 入库是两条独立 effect 链。"""

    knowledge_write: EffectMode = "skip"
    knowledge_provenance: EffectMode = "skip"
    knowledge_title: str = ""
    rag_ingestion: EffectMode = "skip"


@dataclass(frozen=True)
class IntentWorkspace:
    """Workspace 任务的证据、副作用与歧义语义；工具名仍由 Runtime owner 映射。"""

    evidence: WorkspaceEvidenceMode = "skip"
    action: WorkspaceActionMode = "none"
    ambiguity: WorkspaceAmbiguityMode = "clear"
    listing_entry_types: tuple[WorkspaceListingEntryType, ...] = ()
    reason: str = "当前目标不要求访问工作区"


@dataclass(frozen=True)
class RetrievalIntent:
    """一次任务是否需要访问当前 Workspace 的 RAG 文档库。"""

    mode: RetrievalMode
    query: str
    confidence: float
    reason: str
    document_refs: tuple[str, ...] = ()
    document_scope: DocumentScope = "none"
    resolved_document_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class IntentExtraction:
    """IntentExtractor 的版本化输出。"""

    primary_intent: str
    retrieval: RetrievalIntent
    effects: IntentEffects = field(default_factory=IntentEffects)
    workspace: IntentWorkspace = field(default_factory=IntentWorkspace)
    policy_version: str = INTENT_POLICY_VERSION
    source: str = "llm"

    def to_state_dict(self) -> dict:
        value = asdict(self)
        value["retrieval"]["document_refs"] = list(self.retrieval.document_refs)
        value["retrieval"]["resolved_document_ids"] = list(self.retrieval.resolved_document_ids)
        value["workspace"]["listing_entry_types"] = list(
            self.workspace.listing_entry_types
        )
        return value

    @classmethod
    def from_state_dict(cls, value: object) -> "IntentExtraction":
        """Fail closed when restoring a persisted Intent candidate."""
        if not isinstance(value, dict) or set(value) != {
            "primary_intent",
            "retrieval",
            "effects",
            "workspace",
            "policy_version",
            "source",
        }:
            raise ValueError("intent state 结构无效")
        retrieval = value.get("retrieval")
        effects = value.get("effects")
        workspace = value.get("workspace")
        if not isinstance(retrieval, dict) or set(retrieval) != {
            "mode",
            "query",
            "confidence",
            "reason",
            "document_refs",
            "document_scope",
            "resolved_document_ids",
        }:
            raise ValueError("intent retrieval state 结构无效")
        if not isinstance(effects, dict) or set(effects) != {
            "knowledge_write",
            "knowledge_provenance",
            "knowledge_title",
            "rag_ingestion",
        }:
            raise ValueError("intent effects state 结构无效")
        workspace_fields = set(workspace) if isinstance(workspace, dict) else set()
        current_workspace_fields = {
            "evidence",
            "action",
            "ambiguity",
            "listing_entry_types",
            "reason",
        }
        legacy_workspace_fields = current_workspace_fields - {"listing_entry_types"}
        if not isinstance(workspace, dict) or frozenset(workspace_fields) not in {
            frozenset(current_workspace_fields),
            frozenset(legacy_workspace_fields),
        }:
            raise ValueError("intent workspace state 结构无效")
        primary = value.get("primary_intent")
        mode = retrieval.get("mode")
        scope = retrieval.get("document_scope")
        confidence = retrieval.get("confidence")
        refs = retrieval.get("document_refs")
        resolved_ids = retrieval.get("resolved_document_ids")
        if primary not in PRIMARY_INTENTS or mode not in RETRIEVAL_MODES:
            raise ValueError("intent state enum 无效")
        if scope not in DOCUMENT_SCOPES:
            raise ValueError("intent document scope 无效")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= float(confidence) <= 1
        ):
            raise ValueError("intent confidence 无效")
        query = retrieval.get("query")
        reason = retrieval.get("reason")
        if (
            not isinstance(query, str)
            or len(query) > 2_000
            or (mode != "skip" and not query.strip())
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 500
        ):
            raise ValueError("intent query/reason 无效")
        if not _valid_string_list(refs, maximum_items=8, maximum_length=300):
            raise ValueError("intent document refs 无效")
        if not _valid_uuid_list(resolved_ids, maximum_items=20):
            raise ValueError("intent resolved document ids 无效")
        if mode == "skip" and scope != "none":
            raise ValueError("intent skip scope 无效")
        if mode != "skip" and scope == "none":
            raise ValueError("intent retrieval scope 无效")
        if scope == "selected" and not resolved_ids:
            raise ValueError("intent selected scope 缺少文档")
        if scope != "selected" and resolved_ids:
            raise ValueError("intent 非 selected scope 含文档")
        if scope == "unresolved" and (mode != "required" or not refs):
            raise ValueError("intent unresolved scope 无效")
        if primary == "document_question" and (
            mode != "required" or scope not in {"selected", "unresolved"}
        ):
            raise ValueError("intent document question 无效")
        knowledge_write = effects.get("knowledge_write")
        knowledge_provenance = effects.get("knowledge_provenance")
        knowledge_title = effects.get("knowledge_title")
        rag_ingestion = effects.get("rag_ingestion")
        if (
            knowledge_write not in EFFECT_MODES
            or knowledge_provenance not in EFFECT_MODES
            or rag_ingestion not in EFFECT_MODES
        ):
            raise ValueError("intent effects enum 无效")
        if (
            not isinstance(knowledge_title, str)
            or len(knowledge_title) > 200
            or knowledge_title != knowledge_title.strip()
        ):
            raise ValueError("intent knowledge title 无效")
        if knowledge_write == "skip" and (
            knowledge_provenance != "skip" or knowledge_title
        ):
            raise ValueError("intent knowledge effect 语义不一致")
        workspace_evidence = workspace.get("evidence")
        workspace_action = workspace.get("action")
        workspace_ambiguity = workspace.get("ambiguity")
        listing_entry_types = workspace.get("listing_entry_types", [])
        workspace_reason = workspace.get("reason")
        if (
            workspace_evidence not in WORKSPACE_EVIDENCE_MODES
            or workspace_action not in WORKSPACE_ACTION_MODES
            or workspace_ambiguity not in WORKSPACE_AMBIGUITY_MODES
        ):
            raise ValueError("intent workspace enum 无效")
        if (
            not isinstance(workspace_reason, str)
            or not workspace_reason.strip()
            or len(workspace_reason) > 500
        ):
            raise ValueError("intent workspace reason 无效")
        if (
            not _valid_string_list(
                listing_entry_types,
                maximum_items=len(WORKSPACE_LISTING_ENTRY_TYPES),
                maximum_length=20,
            )
            or any(
                item not in WORKSPACE_LISTING_ENTRY_TYPES
                for item in listing_entry_types
            )
        ):
            raise ValueError("intent workspace listing entry types 无效")
        _validate_workspace_semantics(
            workspace_evidence,
            workspace_action,
            workspace_ambiguity,
            tuple(listing_entry_types),
        )
        policy_version = value.get("policy_version")
        if policy_version not in {INTENT_POLICY_VERSION, *LEGACY_INTENT_POLICY_VERSIONS}:
            raise ValueError("intent policy version 无效")
        if policy_version in LEGACY_INTENT_POLICIES_WITHOUT_LISTING and (
            workspace_fields != legacy_workspace_fields
        ):
            raise ValueError("旧 intent policy 不得包含新 workspace 字段")
        if (
            policy_version
            in {INTENT_POLICY_VERSION, "intent-llm-v6"}
            and workspace_fields != current_workspace_fields
        ):
            raise ValueError("当前 intent policy 缺少 workspace 字段")
        source = value.get("source")
        if source not in {"llm", "rule"}:
            raise ValueError("intent source 无效")
        return cls(
            primary_intent=primary,
            retrieval=RetrievalIntent(
                mode=mode,
                query=query.strip(),
                confidence=float(confidence),
                reason=reason.strip(),
                document_refs=tuple(refs),
                document_scope=scope,
                resolved_document_ids=tuple(resolved_ids),
            ),
            effects=IntentEffects(
                knowledge_write=knowledge_write,
                knowledge_provenance=knowledge_provenance,
                knowledge_title=knowledge_title,
                rag_ingestion=rag_ingestion,
            ),
            workspace=IntentWorkspace(
                evidence=workspace_evidence,
                action=workspace_action,
                ambiguity=workspace_ambiguity,
                listing_entry_types=tuple(listing_entry_types),
                reason=workspace_reason.strip(),
            ),
            policy_version=INTENT_POLICY_VERSION,
            source=source,
        )


def _validate_workspace_semantics(
    evidence: str,
    action: str,
    ambiguity: str,
    listing_entry_types: tuple[str, ...] = (),
) -> None:
    if action == "read" and evidence not in {"metadata", "required"}:
        raise ValueError("workspace read 必须要求成功目录/元数据或内容证据")
    if evidence in {"metadata", "required"} and action != "read":
        raise ValueError("workspace 读取证据只适用于 read 目标")
    if action == "none" and (evidence != "skip" or ambiguity != "clear"):
        raise ValueError("workspace none 语义不一致")
    if ambiguity == "clarification_required" and action not in {"write", "destructive"}:
        raise ValueError("workspace clarification 只适用于写入或破坏性目标")
    if listing_entry_types and (
        evidence != "metadata" or action != "read" or ambiguity != "clear"
    ):
        raise ValueError("workspace listing 类型投影只适用于明确的元数据读取")


def _valid_string_list(value: object, *, maximum_items: int, maximum_length: int) -> bool:
    if not isinstance(value, list) or len(value) > maximum_items:
        return False
    seen: set[str] = set()
    for item in value:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item.strip()) > maximum_length
            or item in seen
        ):
            return False
        seen.add(item)
    return True


def _valid_uuid_list(value: object, *, maximum_items: int) -> bool:
    if not _valid_string_list(value, maximum_items=maximum_items, maximum_length=36):
        return False
    try:
        return all(str(UUID(item)) == item for item in value)
    except ValueError:
        return False


class IntentExtractor(Protocol):
    """领域层端口；生产实现由 LLM 生成候选并做确定性校验。"""

    @property
    def uses_model(self) -> bool: ...

    def extract(
        self,
        user_goal: str,
        *,
        available_tool_names: frozenset[str],
        runtime_context: IntentRuntimeContext = IntentRuntimeContext(),
        history_messages: tuple[dict[str, str], ...] = (),
        validation_feedback: str = "",
    ) -> IntentExtraction: ...


class IntentContextProvider(Protocol):
    """从 Storage/Task 边界构造冻结文档目录，不把数据库交给 AgentRunner。"""

    def load(self, task_id: str) -> IntentRuntimeContext: ...
