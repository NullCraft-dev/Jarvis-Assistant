"""Workspace capability 的 manifests 与 executor bindings 唯一 owner。"""

from __future__ import annotations

from typing import Any

from jarvis_worker.agent.tool_gateway.contracts import ToolManifest
from jarvis_worker.agent.tool_gateway.modules import CapabilityModule, ToolBinding
from jarvis_worker.agent.tools.workspace import (
    execute_workspace_create_directory,
    execute_workspace_create_file,
    execute_workspace_delete_path,
    execute_workspace_get_file_info,
    execute_workspace_list_files,
    execute_workspace_move_path,
    execute_workspace_read_file,
    execute_workspace_read_files,
    execute_workspace_search_files,
    execute_workspace_search_text,
)

WORKSPACE_CAPABILITY_ID = "workspace"
WORKSPACE_CAPABILITY_VERSION = "1.2.0"


def _metadata(
    *,
    guidance: str,
    example_arguments: dict[str, Any],
    example_reason: str,
) -> dict[str, Any]:
    return {
        "capability": {
            "id": WORKSPACE_CAPABILITY_ID,
            "version": WORKSPACE_CAPABILITY_VERSION,
        },
        "agent_prompt": {
            "guidance": guidance,
            "example": {
                "arguments": example_arguments,
                "reason": example_reason,
            },
        },
    }


def create_workspace_capability() -> CapabilityModule:
    """创建 Workspace capability 声明。

    每次返回新的 manifest 对象，避免测试或启动装配之间共享可变 metadata/schema。
    """
    bindings = (
        ToolBinding(
            ToolManifest(
                name="workspace.list_files",
                provider="native",
                description="列出指定 workspace 路径下的顶层文件/目录（不递归，最多 100 条，排除 node_modules/.git 等噪声目录）",
                risk_level_default="L0",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "path": {
                            "type": "string",
                            "description": "相对 workspace 的子路径，默认 '.'",
                        },
                    },
                    "required": ["workspace_root"],
                    "additionalProperties": False,
                },
                metadata=_metadata(
                    guidance=(
                        "只有用户明确要求列出/浏览目录，或目标路径确实未知时，才调用 "
                        "workspace.list_files。代码或文档正文检索应优先使用 workspace.search_text；"
                        "不要用逐级目录浏览代替正文检索"
                    ),
                    example_arguments={},
                    example_reason="用户要求列出 workspace 文件",
                ),
            ),
            execute_workspace_list_files,
        ),
        ToolBinding(
            ToolManifest(
                name="workspace.get_file_info",
                provider="native",
                description="获取 workspace 内单个文件、目录或符号链接的有限元信息，不读取正文、不跟随符号链接。返回相对路径、名称、类型、普通文件大小和修改时间。",
                risk_level_default="L0",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "path": {
                            "type": "string",
                            "description": "要查询的 workspace 相对路径；'.' 表示根目录",
                        },
                    },
                    "required": ["workspace_root", "path"],
                    "additionalProperties": False,
                },
                metadata=_metadata(
                    guidance=(
                        "如果用户只要求查看文件、目录或符号链接的类型、大小、修改时间等元信息，"
                        "必须在当前 Task 对每个目标 path 调用 workspace.get_file_info，不要读取文件正文，"
                        "也不得用历史中相同或类似路径的结果代替当前查询"
                    ),
                    example_arguments={"path": "README.md"},
                    example_reason="用户只要求查看 README.md 的文件元信息",
                ),
            ),
            execute_workspace_get_file_info,
        ),
        ToolBinding(
            ToolManifest(
                name="workspace.create_file",
                provider="native",
                description="在 workspace 内安全创建新 UTF-8 文本文件。只创建新文件，不覆盖已有文件，不自动创建父目录。单文件最大 1 MiB。L2 操作需要用户批准。",
                risk_level_default="L2",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "path": {
                            "type": "string",
                            "description": "相对 workspace 的新文件路径（必填）",
                        },
                        "content": {
                            "type": "string",
                            "description": "UTF-8 文件内容（必填）",
                        },
                    },
                    "required": ["workspace_root", "path", "content"],
                    "additionalProperties": False,
                },
                allowed_decisions=["allow_once", "deny"],
                metadata=_metadata(
                    guidance="如果用户明确要求创建文本文件，必须调用 workspace.create_file；不得覆盖已有文件",
                    example_arguments={"path": "notes.txt", "content": ""},
                    example_reason="用户要求创建一个新文本文件",
                ),
            ),
            execute_workspace_create_file,
        ),
        ToolBinding(
            ToolManifest(
                name="workspace.create_directory",
                provider="native",
                description="在 workspace 内安全创建一个新空目录。不覆盖已有路径，也不递归创建父目录。L2 操作需要用户批准。",
                risk_level_default="L2",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "path": {
                            "type": "string",
                            "description": "相对 workspace 的新目录路径（必填）",
                        },
                    },
                    "required": ["workspace_root", "path"],
                    "additionalProperties": False,
                },
                allowed_decisions=["allow_once", "deny"],
                metadata=_metadata(
                    guidance="如果用户明确要求创建目录，必须调用 workspace.create_directory；不得自动补建父目录",
                    example_arguments={"path": "notes"},
                    example_reason="用户要求创建目录",
                ),
            ),
            execute_workspace_create_directory,
        ),
        ToolBinding(
            ToolManifest(
                name="workspace.read_file",
                provider="native",
                description="安全读取 workspace 内单个文本文件，受路径边界、文件大小（默认 64KB，最大 256KB）和字符数（默认 20K，最大 100K）限制，仅支持 UTF-8 文本",
                risk_level_default="L0",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "path": {
                            "type": "string",
                            "description": "相对 workspace 的文件路径（必填）",
                        },
                        "max_bytes": {
                            "type": "integer",
                            "description": "最大读取字节数（可选，受系统上限约束）",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "最大返回字符数（可选，受系统上限约束）",
                        },
                        "start_line": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "可选起始行（1-based）；与搜索结果行号配合读取目标片段",
                        },
                        "max_lines": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1000,
                            "description": "可选最大行数；指定行范围时默认 200，上限 1000",
                        },
                    },
                    "required": ["workspace_root", "path"],
                    "additionalProperties": False,
                },
                metadata=_metadata(
                    guidance=(
                        "读取单个已知文件时调用 workspace.read_file；正文搜索给出行号后，优先使用 "
                        "start_line/max_lines 读取目标附近片段，避免因大文件只读开头或提高 max_bytes 后"
                        "仍拿不到直接证据。需要读取多个候选时改用 workspace.read_files"
                    ),
                    example_arguments={"path": "README.md"},
                    example_reason="用户要求读取 README.md",
                ),
            ),
            execute_workspace_read_file,
        ),
        ToolBinding(
            ToolManifest(
                name="workspace.read_files",
                provider="native",
                description=(
                    "一次安全读取 workspace 内最多 6 个已定位 UTF-8 文本文件或行范围。"
                    "每个条目独立执行路径、symlink、大小和编码校验；允许部分成功并返回逐项错误。"
                ),
                risk_level_default="L0",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 6,
                            "description": (
                                "已由搜索或目录结果定位的精确相对路径/行范围，按输入顺序返回；"
                                "整文件默认范围可直接传路径字符串"
                            ),
                            "items": {
                                "oneOf": [
                                    {
                                        "type": "string",
                                        "description": (
                                            "精确 workspace 相对文件路径简写；可用 path:start:end "
                                            "表示包含首尾行的范围"
                                        ),
                                    },
                                    {
                                        "type": "object",
                                        "properties": {
                                            "path": {
                                                "type": "string",
                                                "description": "精确 workspace 相对文件路径",
                                            },
                                            "start_line": {
                                                "type": "integer",
                                                "minimum": 1,
                                                "description": "可选起始行（1-based），默认 1",
                                            },
                                            "max_lines": {
                                                "type": "integer",
                                                "minimum": 1,
                                                "maximum": 400,
                                                "description": "可选最大行数，默认 200，上限 400",
                                            },
                                        },
                                        "required": ["path"],
                                        "additionalProperties": False,
                                    },
                                ]
                            },
                        },
                    },
                    "required": ["workspace_root", "files"],
                    "additionalProperties": False,
                },
                metadata=_metadata(
                    guidance=(
                        "代码审查、调用链或跨层分析在 workspace.search_text 定位多个候选后，应优先用 "
                        "workspace.read_files 一次读取 2–6 个权威文件或命中行附近片段；不要为每个文件"
                        "分别消耗一次 read_file。path 必须原样复制搜索/目录 ToolResult 返回的精确相对路径，"
                        "不得凭记忆补写或删减目录；范围使用 object 的 start_line/max_lines，或仅在精确路径"
                        "后追加 :start:end；也不要把搜索预览冒充完整文件证据"
                    ),
                    example_arguments={
                        "files": [
                            {"path": "apps/web/src/api/client.ts", "start_line": 1, "max_lines": 200},
                            {"path": "apps/gateway/internal/api/handlers/task.go", "start_line": 1, "max_lines": 240},
                        ]
                    },
                    example_reason="已定位多个调用链 owner，批量读取直接源码证据",
                ),
            ),
            execute_workspace_read_files,
        ),
        ToolBinding(
            ToolManifest(
                name="workspace.search_files",
                provider="native",
                description="在 workspace 内递归搜索匹配名称的文件和目录（只搜索文件名/目录名，不搜索正文）。query 为大小写不敏感的普通 substring，不支持 regex/glob。不跟随 symlink 目录。",
                risk_level_default="L0",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "query": {
                            "type": "string",
                            "description": "搜索关键词，大小写不敏感 substring 匹配（非 regex/glob）",
                        },
                        "path": {
                            "type": "string",
                            "description": "搜索起始子目录的相对路径，默认 '.'",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "最大返回结果数，默认 50，上限 100",
                        },
                    },
                    "required": ["workspace_root", "query"],
                    "additionalProperties": False,
                },
                metadata=_metadata(
                    guidance=(
                        "如果用户要求按名称、扩展名或相对路径查找文件或目录，必须调用 "
                        "workspace.search_files；该工具不搜索文件正文，query 是普通 substring，"
                        "不是 regex/glob。代码库或大型目录中应优先限定 path 并选择能命中文件名的具体"
                        "关键词；结果截断时不要继续用同类宽泛关键词重复搜索。找到候选文件后，若目标"
                        "需要正文证据，应直接调用 workspace.read_file，而不是继续逐级 list_files"
                    ),
                    example_arguments={"query": ".md", "path": ".", "max_results": 50},
                    example_reason="用户要求查找 workspace 内的 Markdown 文件",
                ),
            ),
            execute_workspace_search_files,
        ),
        ToolBinding(
            ToolManifest(
                name="workspace.search_text",
                provider="native",
                description=(
                    "在 workspace 内受支持的 UTF-8 文本文件正文中执行有界关键词搜索。"
                    "query 为大小写不敏感的普通 substring，不支持 regex/glob；"
                    "返回相对路径、行号和有界预览，不跟随 symlink。"
                ),
                risk_level_default="L0",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "query": {
                            "type": "string",
                            "description": "正文关键词，大小写不敏感 substring 匹配（非 regex/glob）",
                        },
                        "path": {
                            "type": "string",
                            "description": "搜索起始文件或子目录的相对路径，默认 '.'",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "最大返回匹配行数，默认 20，上限 50",
                        },
                        "source_only": {
                            "type": "boolean",
                            "description": "代码取证时设为 true，仅扫描生产源码并排除 docs/tests",
                        },
                    },
                    "required": ["workspace_root", "query"],
                    "additionalProperties": False,
                },
                metadata=_metadata(
                    guidance=(
                        "如果目标要求在项目、代码或本地文档正文中定位概念、符号、配置或实现，"
                        "优先调用 workspace.search_text；不要把正文关键词传给只搜索名称的 "
                        "workspace.search_files。代码取证设置 source_only=true，并从 path='.' 或已知"
                        "上层目录搜索一个具体标识符；根据命中路径读取少量权威文件。0 条结果只表示"
                        "精确子串未命中，应换用更短且有区分度的关键词，而不是逐层遍历目录。"
                        "ToolResult 的 scan_complete=true 表示该 path 已完整扫描；"
                        "result_window_truncated=true 只表示返回窗口有限，不存在分页游标，重复相同 "
                        "query/path 不会得到下一页，应缩小 query/path 或读取已返回的候选"
                    ),
                    example_arguments={
                        "query": "CreateTask",
                        "path": ".",
                        "max_results": 20,
                        "source_only": True,
                    },
                    example_reason="用户要求定位代码中的真实实现与调用者",
                ),
            ),
            execute_workspace_search_text,
        ),
        ToolBinding(
            ToolManifest(
                name="workspace.move_path",
                provider="native",
                description="在同一 workspace 内原子移动普通文件、目录或符号链接。目标必须不存在，不覆盖已有路径，不跨设备复制。L3 操作需要用户批准。",
                risk_level_default="L3",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "source_path": {
                            "type": "string",
                            "description": "源相对路径（必填）",
                        },
                        "destination_path": {
                            "type": "string",
                            "description": "目标相对路径（必填）",
                        },
                    },
                    "required": ["workspace_root", "source_path", "destination_path"],
                    "additionalProperties": False,
                },
                allowed_decisions=["allow_once", "deny"],
                metadata=_metadata(
                    guidance="如果用户明确要求移动或重命名路径，必须调用 workspace.move_path；目标路径不得已存在",
                    example_arguments={
                        "source_path": "draft.txt",
                        "destination_path": "archive/draft.txt",
                    },
                    example_reason="用户要求移动文件",
                ),
            ),
            execute_workspace_move_path,
        ),
        ToolBinding(
            ToolManifest(
                name="workspace.delete_path",
                provider="native",
                description="删除 workspace 内的一个普通文件、符号链接或空目录。不删除 workspace 根目录，不执行递归删除。L4 操作每次都需要用户批准。",
                risk_level_default="L4",
                permission_scope="workspace",
                input_schema={
                    "type": "object",
                    "properties": {
                        "workspace_root": {
                            "type": "string",
                            "description": "workspace 根目录（系统自动注入，不可由模型提供）",
                        },
                        "path": {
                            "type": "string",
                            "description": "相对 workspace 的目标路径（必填）",
                        },
                    },
                    "required": ["workspace_root", "path"],
                    "additionalProperties": False,
                },
                allowed_decisions=["allow_once", "deny"],
                metadata=_metadata(
                    guidance="如果用户明确要求删除路径，必须调用 workspace.delete_path；它不递归删除非空目录，且每次必须确认",
                    example_arguments={"path": "obsolete.txt"},
                    example_reason="用户要求删除文件",
                ),
            ),
            execute_workspace_delete_path,
        ),
    )

    return CapabilityModule(
        capability_id=WORKSPACE_CAPABILITY_ID,
        version=WORKSPACE_CAPABILITY_VERSION,
        tool_bindings=bindings,
    )
