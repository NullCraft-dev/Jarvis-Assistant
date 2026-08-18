"""有界、可测试的规则式 IntentExtractor。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from jarvis_worker.agent.core.conversation_constraints import (
    is_citation_verification_goal,
    is_prior_answer_transform_goal,
)
from jarvis_worker.agent.intents.contracts import (
    IntentExtraction,
    IntentRuntimeContext,
    IntentWorkspace,
    RetrievalIntent,
)
from jarvis_worker.agent.intents.workspace_listing import (
    explicit_workspace_listing_entry_types,
)

MAX_INTENT_GOAL_CHARS = 10_000
MAX_RETRIEVAL_QUERY_CHARS = 2_000

_RAG_REFERENCE = re.compile(
    r"(?:rag|知识库|文档库|向量库|向量数据库|已上传(?:的)?(?:文档|文件)|"
    r"当前(?:的)?\s*(?:文档|文件|报告|论文)|这份\s*(?:文档|文件|报告|论文|pdf)|"
    r"那份\s*(?:文档|文件|报告|论文|pdf)|刚才(?:上传|提到)(?:的)?\s*(?:文档|文件|报告|论文)?)",
    re.IGNORECASE,
)
_REQUIRED_RELATION = re.compile(
    r"(?:根据|依据|基于|查询|搜索|检索|查找|来自|文中|文件中|文档中|报告中|论文中)",
    re.IGNORECASE,
)
_EXPLICIT_SKIP = re.compile(
    r"(?:不要|无需|不需要|禁止)\s*"
    r"(?:使用|查询|搜索|检索|访问|提交|写入|加入|存入|导入|上传到|向量化)?\s*"
    r"(?:rag|知识库|文档库|向量库|向量数据库)",
    re.IGNORECASE,
)
_EXPLICIT_NO_TOOL = re.compile(
    r"(?:不要|无需|不需要|禁止)\s*(?:调用|使用|执行)\s*(?:任何|任意|外部)?\s*工具",
    re.IGNORECASE,
)
_RAG_INGESTION = re.compile(
    r"(?:(?:加入|存入|导入|写入|上传到|放入|提交|送入)\s*(?:rag|向量库|向量数据库)|"
    r"(?:rag|向量库|向量数据库)\s*(?:入库|摄取|导入|索引)|"
    r"(?:向量化|建立向量索引|创建向量索引))",
    re.IGNORECASE,
)
_TRANSFORM_ONLY = re.compile(
    r"^(?:请|帮我|麻烦)?\s*(?:润色|改写|翻译|压缩|扩写|纠正|格式化)(?:下面|以下|这段|这句话)",
    re.IGNORECASE,
)
_SOCIAL_ONLY = re.compile(
    r"^(?:你好|您好|嗨|hello|hi|谢谢|感谢|再见)[！!。.?？\s]*$",
    re.IGNORECASE,
)
_KNOWLEDGE_QUESTION = re.compile(
    r"(?:什么是|是什么意思|为什么|为何|原理|机制|区别|差异|对比|比较|优缺点|"
    r"作用|如何实现|怎么实现|怎样实现|如何工作|怎么工作|解释|介绍|最佳实践|"
    r"架构|算法|模型|技术|公式|定理|论文)",
    re.IGNORECASE,
)
_WORKSPACE_REFERENCE = re.compile(r"(?:工作区|workspace)", re.IGNORECASE)
_WORKSPACE_BARE_DIRECTORY_SCOPE = re.compile(
    r"(?<![\w./-])[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\s*(?:下|中|目录|文件夹)",
    re.IGNORECASE,
)
_WORKSPACE_OPT_OUT = re.compile(
    r"(?:不要|无需|不需要|禁止)\s*(?:访问|读取|查看|搜索|检索|使用)?\s*(?:工作区|workspace)",
    re.IGNORECASE,
)
_WORKSPACE_READ_SIGNAL = re.compile(
    r"(?:阅读|读取|查看|查找|搜索|检索|核对|对照|比较|依据|证据|材料|资料|文件|文档|记录|"
    r"列出|有哪些|目录|read|search|find|list|compare|evidence|documents?|files?)",
    re.IGNORECASE,
)
_WORKSPACE_CONTENT_SIGNAL = re.compile(
    r"(?:阅读|读取|正文|内容|核对|对照|比较|依据|证据|材料|资料|文档|记录|政策|流程|"
    r"read|compare|evidence|content|documents?|records?|polic(?:y|ies)|process)",
    re.IGNORECASE,
)
_WORKSPACE_CONTENT_SEARCH_SIGNAL = re.compile(
    r"(?:搜索|查找|检索|找出|找一下|哪些.{0,30}(?:包含|含有)|"
    r"\b(?:search|find|locate)\b.{0,80}\b(?:text|string|symbol|identifier|files?)\b|"
    r"\b(?:files?|sources?)\b.{0,80}\bcontain(?:s|ing)?\b)",
    re.IGNORECASE | re.DOTALL,
)
_WORKSPACE_EFFECT_SIGNAL = re.compile(
    r"(?:创建|新建|写入|保存|移动|重命名|删除|清理|覆盖|修改|更新|"
    r"create|write|save|move|rename|delete|remove|overwrite|modify|update)",
    re.IGNORECASE,
)
_WORKSPACE_NEGATED_EFFECT_SIGNAL = re.compile(
    r"(?:不要|无需|不需要|禁止)\s*(?:实际)?\s*"
    r"(?:创建|新建|写入|保存|移动|重命名|删除|清理|覆盖|修改|更新|"
    r"create|write|save|move|rename|delete|remove|overwrite|modify|update)",
    re.IGNORECASE,
)
_WORKSPACE_MOVE_SIGNAL = re.compile(r"(?:移动|重命名|\bmove\b|\brename\b)", re.IGNORECASE)
_WORKSPACE_DELETE_SIGNAL = re.compile(r"(?:删除|移除|清理|\bdelete\b|\bremove\b)", re.IGNORECASE)
_WORKSPACE_CREATE_DIRECTORY_SIGNAL = re.compile(
    r"(?:(?:创建|新建).{0,24}(?:目录|文件夹)|"
    r"(?:create|make).{0,24}(?:director(?:y|ies)|folders?))",
    re.IGNORECASE,
)
_WORKSPACE_CREATE_FILE_SIGNAL = re.compile(
    r"(?:(?:创建|新建|写入|保存).{0,40}(?:文件|内容|[.][A-Za-z0-9]{1,16})|"
    r"(?:create|write|save).{0,40}(?:files?|[.][A-Za-z0-9]{1,16}))",
    re.IGNORECASE,
)
_WORKSPACE_BROAD_EFFECT_SCOPE = re.compile(
    r"(?:所有|全部|任意|任何|批量|重复文件|里面的内容|目录和里面|"
    r"\ball\b|\bany\b|\bevery\b|\bduplicates?\b)",
    re.IGNORECASE,
)
_WORKSPACE_UNRESOLVED_EFFECT_REFERENCE = re.compile(
    r"(?:删掉它|删除它|移动它|重命名它|覆盖它|这些文件|那些文件|"
    r"\bdelete it\b|\bmove it\b|\brename it\b|\bthese files\b|\bthose files\b)",
    re.IGNORECASE,
)
_EXPLICIT_SCOPED_DIRECTORY_DELETE = re.compile(
    r"(?:^|[，,。！？!?；;：:\n])\s*"
    r"(?:请(?:帮我)?|帮我|麻烦(?:你)?|务必|必须)?\s*"
    r"(?:删除|移除)\s*"
    r"(?:当前)?(?:工作区|workspace)?\s*(?:中|内|里的)?\s*"
    r"(?P<path>[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_.\-\u4e00-\u9fff]{0,99}"
    r"(?:/[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_.\-\u4e00-\u9fff]{0,99})*)\s*"
    r"(?:目录|文件夹)\s*"
    r"(?:(?:以及|及|和|连同)\s*(?:里面|其中|目录内|文件夹内)?(?:的)?\s*"
    r"(?:所有|全部)\s*(?:内容|文件|子目录|文件和子目录)?)?\s*"
    r"(?:[。.!！]|$)",
    re.IGNORECASE,
)
_RECENT_CREATED_DIRECTORY_REFERENCE = re.compile(
    r"(?:刚建的目录|刚才(?:创建|新建)的目录|新建的目录|"
    r"(?:directory|folder)\s+(?:just|recently)\s+created)",
    re.IGNORECASE,
)
_CONFIRMED_CREATED_DIRECTORY = re.compile(
    r"(?:已(?:成功)?(?:在[^，。；\n]{1,30}(?:下|中))?创建(?:了)?目录|"
    r"目录.{0,12}已(?:成功)?创建|"
    r"(?:created|made)\s+(?:the\s+)?(?:directory|folder))",
    re.IGNORECASE,
)
_RELATIVE_PATH_TOKEN = re.compile(
    r"(?<![\w.-])(?:[\w-]+/)+[\w.-]+|(?<![\w.-])[\w-]+[.][A-Za-z0-9]{1,16}(?![\w.-])",
    re.UNICODE,
)


class RuleBasedIntentExtractor:
    """仅供离线测试/显式降级使用的旧实现；生产不再装配。"""

    @property
    def uses_model(self) -> bool:
        return False

    def extract(
        self,
        user_goal: str,
        *,
        available_tool_names: frozenset[str],
        runtime_context: IntentRuntimeContext = IntentRuntimeContext(),
        history_messages: tuple[dict[str, str], ...] = (),
        validation_feedback: str = "",
    ) -> IntentExtraction:
        del runtime_context, history_messages, validation_feedback
        goal = user_goal.strip()[:MAX_INTENT_GOAL_CHARS] if isinstance(user_goal, str) else ""
        query = goal[:MAX_RETRIEVAL_QUERY_CHARS]

        if "rag.search" not in available_tool_names:
            return _result("task", "skip", query, 1.0, "当前运行未提供 rag.search")
        if not goal:
            return _result("unknown", "skip", query, 1.0, "用户目标为空")
        if _EXPLICIT_NO_TOOL.search(goal):
            return _result("task", "skip", query, 0.99, "用户明确要求不调用工具")
        if _EXPLICIT_SKIP.search(goal):
            return _result("task", "skip", query, 0.99, "用户明确要求不访问 RAG")
        if _RAG_INGESTION.search(goal):
            return _result(
                "task",
                "skip",
                query,
                0.98,
                "当前目标是提交 RAG 摄取，不是查询已有 RAG 文档",
            )
        if is_citation_verification_goal(goal):
            return _result(
                "document_question",
                "required",
                query,
                1.0,
                "当前目标要求核对上一轮引用与结论的支持关系",
                (goal[:300],),
            )

        rag_reference = _RAG_REFERENCE.search(goal)
        if rag_reference and (_REQUIRED_RELATION.search(goal) or "?" in goal or "？" in goal):
            return _result(
                "document_question",
                "required",
                query,
                0.98,
                "问题明确依赖已入库文档或知识库",
                (rag_reference.group(0),),
            )
        if (
            _SOCIAL_ONLY.search(goal)
            or _TRANSFORM_ONLY.search(goal)
            or is_prior_answer_transform_goal(goal)
        ):
            return _result("conversation", "skip", query, 0.96, "当前请求不需要外部知识证据")
        if _KNOWLEDGE_QUESTION.search(goal):
            return _result(
                "knowledge_question",
                "retrieve",
                query,
                0.82,
                "专业知识问题可能从当前 Workspace 文档中获益",
            )
        return _result("task", "skip", query, 0.60, "规则未发现可靠的检索需求")


def is_explicit_workspace_content_search_goal(user_goal: str) -> bool:
    """仅识别用户明确要求按正文查询词定位命中的 Workspace 任务。"""
    return bool(
        isinstance(user_goal, str)
        and _WORKSPACE_CONTENT_SEARCH_SIGNAL.search(user_goal[:MAX_INTENT_GOAL_CHARS])
    )


def build_safe_workspace_read_fallback(
    user_goal: str,
    *,
    available_tool_names: frozenset[str],
) -> IntentExtraction | None:
    """为结构化 Intent 穷尽失败提供仅限 L0 Workspace 读取的安全降级。

    该降级不推断写入、移动、删除、知识沉淀或 RAG 入库语义，也不固定业务路径、
    查询词或答案。只有原始目标明确提到 Workspace，或提供安全相对路径/目录范围，
    并同时具有读取/证据信号且没有副作用动词时才返回规则 Intent。
    """
    if not isinstance(user_goal, str) or not user_goal.strip():
        return None
    goal = user_goal.strip()[:MAX_INTENT_GOAL_CHARS]
    effect_goal = _WORKSPACE_NEGATED_EFFECT_SIGNAL.sub("", goal)
    has_scoped_reference = bool(
        _WORKSPACE_REFERENCE.search(goal)
        or _extract_relative_paths(goal)
        or _WORKSPACE_BARE_DIRECTORY_SCOPE.search(goal)
    )
    if (
        not has_scoped_reference
        or not _WORKSPACE_READ_SIGNAL.search(goal)
        or _WORKSPACE_OPT_OUT.search(goal)
        or _WORKSPACE_EFFECT_SIGNAL.search(effect_goal)
        or _has_unsafe_path_syntax(goal)
    ):
        return None
    content_read = bool(
        _WORKSPACE_CONTENT_SIGNAL.search(goal) or is_explicit_workspace_content_search_goal(goal)
    )
    required_tools = (
        {"workspace.read_file", "workspace.read_files"}
        if content_read
        else {"workspace.list_files", "workspace.get_file_info"}
    )
    if content_read and is_explicit_workspace_content_search_goal(goal):
        required_tools.add("workspace.search_text")
    if not available_tool_names.intersection(required_tools):
        return None
    evidence = "required" if content_read else "metadata"
    listing_entry_types = (
        explicit_workspace_listing_entry_types(goal) if evidence == "metadata" else ()
    )
    return IntentExtraction(
        primary_intent="task",
        retrieval=RetrievalIntent(
            mode="skip",
            query="",
            confidence=1.0,
            reason="结构化 Intent 穷尽失败后，仅恢复明确的只读 Workspace 目标",
            document_scope="none",
        ),
        workspace=IntentWorkspace(
            evidence=evidence,
            action="read",
            ambiguity="clear",
            listing_entry_types=listing_entry_types,
            reason="原始目标明确要求读取或核对当前 Workspace，且未包含副作用动作",
        ),
        source="rule",
    )


def build_safe_workspace_effect_fallback(
    user_goal: str,
    *,
    available_tool_names: frozenset[str],
    history_messages: tuple[dict[str, str], ...] = (),
) -> IntentExtraction | None:
    """在 Intent 模型穷尽失败后恢复 Workspace 副作用的最小安全语义。

    该层只恢复 ``write|destructive`` 及 ``clear|clarification_required``，不生成
    ToolRequest、路径参数或权限决定。明确范围仍须经过动作模型、ToolGateway 和对应
    L2/L3/L4 权限；含糊范围只会进入 Runtime 的确定性澄清，不会请求权限或执行工具。
    """
    if not isinstance(user_goal, str) or not user_goal.strip():
        return None
    goal = user_goal.strip()[:MAX_INTENT_GOAL_CHARS]
    if (
        _WORKSPACE_OPT_OUT.search(goal)
        or _EXPLICIT_NO_TOOL.search(goal)
        or not _WORKSPACE_EFFECT_SIGNAL.search(goal)
        or _has_unsafe_path_syntax(goal)
    ):
        return None

    effect_kinds = []
    if _WORKSPACE_MOVE_SIGNAL.search(goal):
        effect_kinds.append("move")
    if _WORKSPACE_DELETE_SIGNAL.search(goal):
        effect_kinds.append("delete")
    if _WORKSPACE_CREATE_DIRECTORY_SIGNAL.search(goal):
        effect_kinds.append("create_directory")
    elif _WORKSPACE_CREATE_FILE_SIGNAL.search(goal):
        effect_kinds.append("create_file")
    if len(effect_kinds) != 1:
        return None

    effect_kind = effect_kinds[0]
    required_tool = {
        "move": "workspace.move_path",
        "delete": "workspace.delete_path",
        "create_directory": "workspace.create_directory",
        "create_file": "workspace.create_file",
    }[effect_kind]
    if required_tool not in available_tool_names:
        return None

    paths = _extract_relative_paths(goal)
    explicit_scoped_directory_delete = bool(
        effect_kind == "delete" and is_explicit_scoped_directory_delete_goal(goal)
    )
    broad_or_unresolved = bool(
        (_WORKSPACE_BROAD_EFFECT_SCOPE.search(goal) and not explicit_scoped_directory_delete)
        or _WORKSPACE_UNRESOLVED_EFFECT_REFERENCE.search(goal)
    )
    resolved_history_directory = ""
    if effect_kind == "move" and _RECENT_CREATED_DIRECTORY_REFERENCE.search(goal):
        resolved_history_directory = _latest_confirmed_created_directory(history_messages)

    if effect_kind == "move":
        clear = not broad_or_unresolved and (
            len(paths) >= 2 or (len(paths) == 1 and bool(resolved_history_directory))
        )
    else:
        clear = not broad_or_unresolved and bool(paths or explicit_scoped_directory_delete)

    action = "destructive" if effect_kind == "delete" else "write"
    ambiguity = "clear" if clear else "clarification_required"
    scope_reason = (
        f"；会话中唯一已确认的新目录为 {resolved_history_directory}"
        if resolved_history_directory
        else ""
    )
    return IntentExtraction(
        primary_intent="task",
        retrieval=RetrievalIntent(
            mode="skip",
            query="",
            confidence=1.0,
            reason="结构化 Intent 穷尽失败后，仅恢复 Workspace 副作用安全边界",
            document_scope="none",
        ),
        workspace=IntentWorkspace(
            evidence="skip",
            action=action,
            ambiguity=ambiguity,
            reason=(
                f"原始目标可确定性分类为 {effect_kind}；"
                f"副作用范围{'明确' if clear else '仍需澄清'}{scope_reason}"
            ),
        ),
        source="rule",
    )


def is_explicit_scoped_directory_delete_goal(user_goal: str) -> bool:
    """识别“删除一个具名目录及其全部内容”的明确 L4 意图。

    ``所有/全部`` 在这里限定于唯一具名目录，并不表示跨目录候选集合。函数只裁决
    Intent 的歧义位，不提取 ToolRequest、不授予权限，也不改变 delete_path 的非递归
    执行契约。路径穿越、通配符、指代词和“删除目录中的部分候选”仍保持 fail closed。
    """
    if not isinstance(user_goal, str) or not user_goal.strip():
        return False
    goal = user_goal.strip()[:MAX_INTENT_GOAL_CHARS]
    if (
        _has_unsafe_path_syntax(goal)
        or _WORKSPACE_OPT_OUT.search(goal)
        or _EXPLICIT_NO_TOOL.search(goal)
        or _WORKSPACE_UNRESOLVED_EFFECT_REFERENCE.search(goal)
    ):
        return False
    matches = tuple(_EXPLICIT_SCOPED_DIRECTORY_DELETE.finditer(goal))
    if len(matches) != 1:
        return False
    target = matches[0].group("path")
    return target not in {"这", "这些", "那", "那些", "该", "这个", "那个"}


def build_safe_workspace_fallback(
    user_goal: str,
    *,
    available_tool_names: frozenset[str],
    history_messages: tuple[dict[str, str], ...] = (),
) -> IntentExtraction | None:
    """组合只读与副作用安全恢复；两者都不绕过 ToolGateway 或权限。"""
    read_fallback = build_safe_workspace_read_fallback(
        user_goal,
        available_tool_names=available_tool_names,
    )
    if read_fallback is not None:
        return read_fallback
    return build_safe_workspace_effect_fallback(
        user_goal,
        available_tool_names=available_tool_names,
        history_messages=history_messages,
    )


def build_safe_intent_fallback(
    user_goal: str,
    *,
    available_tool_names: frozenset[str],
    history_messages: tuple[dict[str, str], ...] = (),
) -> IntentExtraction | None:
    """结构化 Intent 穷尽失败后的最小安全恢复。

    明确的 Workspace 读写仍由既有规则恢复。其余非空目标只能降级为
    ``unknown``，交给 Loop 的 host-owned 澄清终点；该契约不授予任何工具、
    RAG、Knowledge 或副作用能力。
    """
    workspace_fallback = build_safe_workspace_fallback(
        user_goal,
        available_tool_names=available_tool_names,
        history_messages=history_messages,
    )
    if workspace_fallback is not None:
        return workspace_fallback
    if not isinstance(user_goal, str) or not user_goal.strip():
        return None
    return IntentExtraction(
        primary_intent="unknown",
        retrieval=RetrievalIntent(
            mode="skip",
            query="",
            confidence=1.0,
            reason="结构化 Intent 穷尽失败后进入无能力澄清边界",
            document_scope="none",
        ),
        workspace=IntentWorkspace(
            evidence="skip",
            action="none",
            ambiguity="clear",
            reason="未形成可信任务语义；禁止工具调用并要求用户补充目标",
        ),
        source="rule",
    )


def _extract_relative_paths(value: str) -> tuple[str, ...]:
    result: list[str] = []
    for match in _RELATIVE_PATH_TOKEN.finditer(value):
        candidate = match.group(0).strip("`'\"，。；：,;:()（）[]【】")
        path = PurePosixPath(candidate)
        if (
            not candidate
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or str(path) != candidate
            or candidate in result
        ):
            continue
        result.append(candidate)
    return tuple(result)


def _has_unsafe_path_syntax(value: str) -> bool:
    return bool(
        "\x00" in value
        or re.search(r"(?:^|[\s`'\"（(])(?:[~]|[.][.](?:/|\\)|/)", value)
        or re.search(r"[*?{}]", value)
        or "://" in value
    )


def _latest_confirmed_created_directory(
    history_messages: tuple[dict[str, str], ...],
) -> str:
    recent_messages = history_messages[-6:]
    for index in range(len(recent_messages) - 1, -1, -1):
        message = recent_messages[index]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not _CONFIRMED_CREATED_DIRECTORY.search(content):
            continue
        paths = _extract_relative_paths(content)
        if len(paths) != 1:
            return ""
        candidate = paths[0]
        for prior_message in reversed(recent_messages[:index]):
            if not isinstance(prior_message, dict) or prior_message.get("role") != "user":
                continue
            prior_content = prior_message.get("content")
            if not isinstance(prior_content, str):
                return ""
            prior_paths = _extract_relative_paths(prior_content)
            if (
                _WORKSPACE_CREATE_DIRECTORY_SIGNAL.search(prior_content)
                and candidate in prior_paths
            ):
                return candidate
            return ""
    return ""


def _result(
    primary_intent: str,
    mode,
    query: str,
    confidence: float,
    reason: str,
    document_refs: tuple[str, ...] = (),
) -> IntentExtraction:
    safe_refs = document_refs or (
        (query[:300],) if primary_intent == "document_question" and query else ()
    )
    document_scope = (
        "unresolved"
        if mode == "required" and safe_refs
        else ("all" if mode in {"retrieve", "required"} else "none")
    )
    return IntentExtraction(
        primary_intent=primary_intent,
        retrieval=RetrievalIntent(
            mode=mode,
            query=query,
            confidence=confidence,
            reason=reason,
            document_refs=safe_refs,
            document_scope=document_scope,
        ),
        source="rule",
    )
