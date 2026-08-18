import json
from uuid import uuid4

from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.runner import AgentRunner
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.intents import (
    IntentDocument,
    IntentRuntimeContext,
    LlmIntentExtractor,
)
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.permissions.manager import PermissionManager
from jarvis_worker.agent.tool_gateway.contracts import ToolManifest, ToolResult
from jarvis_worker.agent.tool_gateway.gateway import ToolGateway
from jarvis_worker.agent.tool_gateway.registry import ToolRegistry
from jarvis_worker.runtime_bus.messages import RunJobMessage


class _HarnessModel(ModelProvider):
    def __init__(self, intent_outputs, actions):
        self.intent_outputs = list(intent_outputs)
        self.actions = list(actions)
        self.intent_calls = 0
        self.action_calls = 0
        self.intent_messages = []

    @property
    def provider_name(self):
        return "intent-test"

    @property
    def model_name(self):
        return "intent-model"

    def complete_structured(self, messages, parser):
        self.intent_messages.append(messages)
        output = self.intent_outputs[self.intent_calls]
        self.intent_calls += 1
        return parser(output)

    def decide_next_action(self, state: AgentState):
        action = self.actions[self.action_calls]
        self.action_calls += 1
        return action

    def decide_next_action_stream(self, state: AgentState, on_text_delta):
        action = self.decide_next_action(state)
        if action.action_type == "finish":
            for index in range(0, len(action.final_message), 7):
                on_text_delta(action.final_message[index : index + 7])
        return action


class _ContextProvider:
    def __init__(self, context):
        self.context = context
        self.calls = []

    def load(self, task_id):
        self.calls.append(task_id)
        return self.context


def _intent_output(
    *,
    scope="none",
    keys=None,
    mode="skip",
    workspace_evidence="skip",
    workspace_action="none",
    workspace_ambiguity="clear",
    listing_entry_types=None,
):
    return json.dumps(
        {
            "primary_intent": (
                "document_question"
                if mode == "required" and scope in {"selected", "unresolved"}
                else "task"
            ),
            "retrieval": {
                "mode": mode,
                "query": "总结这份资料",
                "confidence": 0.95,
                "reason": "任务需要指定资料" if mode == "required" else "无需检索",
                "document_refs": ["这份资料"] if mode == "required" else [],
                "document_scope": scope,
                "document_keys": keys or [],
            },
            "effects": {
                "knowledge_write": "skip",
                "knowledge_provenance": "skip",
                "knowledge_title": "",
                "rag_ingestion": "skip",
            },
            "workspace": {
                "evidence": workspace_evidence,
                "action": workspace_action,
                "ambiguity": workspace_ambiguity,
                "listing_entry_types": listing_entry_types or [],
                "reason": "工作区语义已分类",
            },
        },
        ensure_ascii=False,
    )


def _job(user_goal="总结刚才那份资料"):
    return RunJobMessage(
        job_id="job-intent",
        trace_id="trace-intent",
        task_id="task-intent",
        run_id="run-intent",
        user_goal=user_goal,
        created_at="2026-07-30T00:00:00+00:00",
    )


def _gateway(captured=None):
    registry = ToolRegistry()
    if captured is not None:

        def execute(request):
            captured.append(request)
            return ToolResult(
                ok=True,
                kind="json",
                summary="已检索指定文档",
                data={"results": []},
            )

        registry.register(
            ToolManifest(
                name="rag.search",
                provider="native",
                risk_level_default="L0",
                permission_scope="current_workspace_rag",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "document_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                allowed_decisions=[],
            ),
            execute,
        )
    return ToolGateway(registry, PermissionManager())


def _events(runner):
    return runner.run(_job())


def _workspace_gateway(captured):
    registry = ToolRegistry()

    def execute(request):
        captured.append(request)
        if request.tool_name == "workspace.list_files":
            return ToolResult(
                ok=True,
                kind="json",
                summary="4 个根目录条目",
                data={
                    "entries": [
                        {"name": "apps", "type": "dir"},
                        {"name": "docs", "type": "dir"},
                        {"name": "draft.md", "type": "file"},
                        {"name": "external-link", "type": "symlink"},
                    ]
                },
            )
        return ToolResult(ok=True, kind="text", summary="已读取真实文件", data="content")

    registry.register(ToolManifest(name="workspace.list_files", risk_level_default="L0"), execute)
    registry.register(ToolManifest(name="workspace.read_file", risk_level_default="L0"), execute)
    registry.register(ToolManifest(name="workspace.delete_path", risk_level_default="L4"), execute)
    return ToolGateway(registry, PermissionManager())


def _workspace_search_gateway(captured):
    registry = ToolRegistry()

    def execute(request):
        captured.append(request)
        return ToolResult(
            ok=True,
            kind="json",
            summary="正文搜索完成",
            data={
                "matches": [
                    {
                        "path": "project/src/secrets.ts",
                        "line_number": 1,
                        "preview": 'export const AUTH_TOKEN = "fake-eval-token"',
                    }
                ]
            },
        )

    registry.register(
        ToolManifest(name="workspace.search_text", risk_level_default="L0"), execute
    )
    return ToolGateway(registry, PermissionManager())


def _workspace_move_gateway(captured):
    registry = ToolRegistry()

    def execute(request):
        captured.append(request)
        return ToolResult(ok=True, kind="json", summary="已移动路径", data={})

    registry.register(
        ToolManifest(
            name="workspace.move_path",
            provider="native",
            risk_level_default="L3",
            permission_scope="workspace",
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_root": {"type": "string"},
                    "source_path": {"type": "string"},
                    "destination_path": {"type": "string"},
                },
                "required": ["workspace_root", "source_path", "destination_path"],
                "additionalProperties": False,
            },
            allowed_decisions=["allow_once", "deny"],
        ),
        execute,
    )
    return ToolGateway(registry, PermissionManager())


def test_invalid_intent_candidate_retries_inside_agent_loop_then_continues():
    model = _HarnessModel(
        ["not-json", _intent_output()],
        [AgentAction.finish("已完成")],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_gateway(),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=3,
    )

    envelopes = _events(runner)
    events = [item.runtime_event for item in envelopes]

    assert model.intent_calls == 2
    assert model.action_calls == 1
    assert sum(event["type"] == "model.call.started" for event in events) == 3
    assert any(
        event["type"] == "model.call.failed"
        and event["payload"]["error_code"] == "INTENT_OUTPUT_INVALID"
        and event["payload"]["recoverable"] is True
        for event in events
    )
    assert any(
        event["type"] == "model.call.completed"
        and event["payload"]["action_type"] == "intent_extraction"
        for event in events
    )
    assert events[-1]["type"] == "agent.run.completed"


def test_exhausted_intent_validation_enters_host_owned_clarification_without_tool():
    model = _HarnessModel(
        ["not-json", "still-not-json"],
        [AgentAction.call_tool("workspace.list_files", {"path": "."}, "猜测目标")],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_gateway(),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=3,
    )

    events = [item.runtime_event for item in _events(runner)]

    assert model.intent_calls == 2
    assert model.action_calls == 1
    assert events[-1]["type"] == "agent.run.completed"
    assert "请补充要整理或处理的内容" in events[-1]["payload"]["output"]
    assert not any(event["type"] == "tool.call.started" for event in events)


def test_exhausted_intent_validation_uses_read_only_workspace_fallback():
    captured = []
    model = _HarnessModel(
        ["not-json", "still-not-json"],
        [
            AgentAction.call_tool(
                "workspace.read_file",
                {"path": "notes/today.md"},
                "读取用户明确指定的 Workspace 文件",
            ),
            AgentAction.finish("已根据文件正文完成概括"),
        ],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_workspace_gateway(captured),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=3,
    )

    events = [
        item.runtime_event
        for item in runner.run(_job("请读取 notes/today.md 并概括内容。"))
    ]

    assert model.intent_calls == 2
    assert model.action_calls == 2
    assert [request.tool_name for request in captured] == ["workspace.read_file"]
    assert any(
        event["type"] == "model.call.failed"
        and event["payload"]["error_code"] == "INTENT_OUTPUT_INVALID"
        and event["payload"]["recoverable"] is True
        for event in events
    )
    assert events[-1]["type"] == "agent.run.completed"


def test_explicit_content_search_finishes_from_search_evidence_and_redacts_preview():
    captured = []
    model = _HarnessModel(
        ["not-json", "still-not-json"],
        [
            AgentAction.call_tool(
                "workspace.search_text",
                {"path": "project", "query": "AUTH_TOKEN"},
                "搜索变量名",
            ),
            AgentAction.finish("project/src/secrets.ts 包含 AUTH_TOKEN；未输出任何值。"),
        ],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_workspace_search_gateway(captured),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=3,
    )

    events = [
        item.runtime_event
        for item in runner.run(
            _job(
                "在 project 中搜索字符串 AUTH_TOKEN，并告诉我哪些文件包含这个变量名，"
                "但不要输出任何值。"
            )
        )
    ]

    assert [request.tool_name for request in captured] == ["workspace.search_text"]
    assert model.action_calls == 2
    assert events[-1]["type"] == "agent.run.completed"
    assert "fake-eval-token" not in json.dumps(events, ensure_ascii=False)


def test_exhausted_intent_validation_recovers_explicit_move_until_l3_permission():
    captured = []
    model = _HarnessModel(
        ["not-json", "still-not-json"],
        [
            AgentAction.call_tool(
                "workspace.move_path",
                {
                    "source_path": "incoming/meeting-notes.md",
                    "destination_path": "archive/2026-Q3/meeting-notes.md",
                },
                "移动用户明确指定的单个文件",
            )
        ],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_workspace_move_gateway(captured),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=3,
    )

    envelopes = runner.run(
        _job("把 incoming/meeting-notes.md 移动到刚建的目录，其他文件不动。"),
        history_messages=[
            {
                "role": "user",
                "content": "创建 archive/2026-Q3 目录，但先不要移动文件。",
            },
            {
                "role": "assistant",
                "content": (
                    "已在 workspace 下创建目录 `archive/2026-Q3`，可用于后续整理文件。"
                    "未移动任何文件。"
                ),
            },
        ],
        defer_permission=True,
    )
    events = [item.runtime_event for item in envelopes]

    assert model.intent_calls == 2
    assert model.action_calls == 1
    assert captured == []
    assert events[-1]["type"] == "permission.required"
    request = events[-1]["payload"]["request"]
    assert request["tool_name"] == "workspace.move_path"
    assert request["risk_level"] == "L3"
    assert request["allowed_decisions"] == ["allow_once", "deny"]


def test_selected_document_scope_overwrites_model_supplied_document_ids():
    trusted_id = str(uuid4())
    forged_id = str(uuid4())
    context = IntentRuntimeContext(
        (
            IntentDocument(
                key="doc_1",
                document_id=trusted_id,
                title="可信资料",
                created_at="2026-07-30T00:00:00+00:00",
            ),
        )
    )
    captured = []
    model = _HarnessModel(
        [_intent_output(scope="selected", keys=["doc_1"], mode="required")],
        [
            AgentAction.call_tool(
                "rag.search",
                {"query": "总结资料", "document_ids": [forged_id]},
                "检索指定文档",
            ),
            AgentAction.finish("没有找到足够证据"),
        ],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_gateway(captured),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(context),
        max_iterations=3,
    )

    events = [item.runtime_event for item in _events(runner)]

    assert captured[0].arguments["document_ids"] == [trusted_id]
    assert forged_id not in captured[0].arguments["document_ids"]
    assert events[-1]["type"] == "agent.run.completed"
    intent_prompt = "\n".join(message.content for message in model.intent_messages[0])
    assert trusted_id not in intent_prompt


def test_unresolved_specific_document_finishes_with_clarification_without_rag_search():
    model = _HarnessModel(
        [_intent_output(scope="unresolved", mode="required")],
        [AgentAction.finish("请告诉我具体是哪一份资料。")],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_gateway([]),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=3,
    )

    events = [item.runtime_event for item in _events(runner)]

    assert model.action_calls == 1
    assert not any(event["type"] == "tool.call.started" for event in events)
    assert events[-1]["type"] == "agent.run.completed"


def test_valid_intent_fails_closed_when_runtime_capability_is_missing():
    model = _HarnessModel(
        [_intent_output(scope="all", mode="required")],
        [AgentAction.finish("不能伪装成已检索")],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_gateway(),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=3,
    )

    events = [item.runtime_event for item in _events(runner)]

    assert model.action_calls == 0
    assert events[-1]["type"] == "agent.run.failed"
    assert events[-1]["payload"]["error"]["code"] == "INTENT_CAPABILITY_UNAVAILABLE"


def test_workspace_review_cannot_finish_before_reading_real_file_content():
    captured = []
    model = _HarnessModel(
        [
            _intent_output(
                workspace_evidence="required",
                workspace_action="read",
            )
        ],
        [
            AgentAction.finish("代码已经审查完成，没有问题"),
            AgentAction.call_tool("workspace.read_file", {"path": "src/auth.py"}, "读取真实实现"),
            AgentAction.finish("已基于真实文件完成审查"),
        ],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_workspace_gateway(captured),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=4,
    )

    events = [
        item.runtime_event for item in runner.run(_job("帮我审查项目鉴权实现是否存在越权问题"))
    ]

    assert len(captured) == 1
    assert captured[0].tool_name == "workspace.read_file"
    assert any(
        event["type"] == "model.call.failed"
        and event["payload"]["error_code"] == "REQUIRED_TOOL_EVIDENCE_MISSING"
        for event in events
    )
    assert events[-1]["type"] == "agent.run.completed"
    assert events[-1]["payload"]["output"] == "已基于真实文件完成审查"


def test_workspace_listing_uses_metadata_evidence_and_hides_rejected_draft():
    captured = []
    model = _HarnessModel(
        [
            _intent_output(
                workspace_evidence="metadata",
                workspace_action="read",
                listing_entry_types=["dir"],
            )
        ],
        [
            AgentAction.finish("不应提前展示的目录回答"),
            AgentAction.call_tool("workspace.list_files", {"path": "."}, "读取一级目录"),
            AgentAction.finish("一级目录包括 apps、docs；另有 draft.md 和 external-link"),
            AgentAction.finish("一级目录包括 apps 和 docs"),
        ],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_workspace_gateway(captured),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=4,
    )

    events = [
        item.runtime_event
        for item in runner.run(
            _job("不要创建任何文件。请只告诉我这个工作区根目录下有哪些一级目录。")
        )
    ]

    assert len(captured) == 1
    assert captured[0].tool_name == "workspace.list_files"
    assert any(
        event["type"] == "model.call.failed"
        and event["payload"]["error_code"] == "REQUIRED_TOOL_EVIDENCE_MISSING"
        for event in events
    )
    visible_deltas = "".join(
        event["payload"]["delta"] for event in events if event["type"] == "model.delta"
    )
    assert visible_deltas == ""
    completed = next(event for event in events if event["type"] == "agent.run.completed")
    assert completed["payload"]["output"] == "一级目录包括 apps 和 docs"
    assert "不应提前展示" not in visible_deltas
    assert events[-1]["type"] == "agent.run.completed"
    assert events[-1]["payload"]["output"] == "一级目录包括 apps 和 docs"
    assert any(
        event["type"] == "model.call.failed"
        and event["payload"]["error_code"] == "FINAL_ANSWER_VALIDATION_FAILED"
        for event in events
    )


def test_workspace_metadata_intent_fails_closed_without_metadata_capability():
    registry = ToolRegistry()
    registry.register(
        ToolManifest(name="workspace.read_file", risk_level_default="L0"),
        lambda _request: ToolResult(ok=True, kind="text", data="content"),
    )
    model = _HarnessModel(
        [
            _intent_output(
                workspace_evidence="metadata",
                workspace_action="read",
                listing_entry_types=["dir"],
            )
        ],
        [AgentAction.finish("不能伪装成已经列过目录")],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=ToolGateway(registry, PermissionManager()),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=3,
    )

    events = [item.runtime_event for item in runner.run(_job("列出根目录的一级目录"))]

    assert model.action_calls == 0
    assert events[-1]["type"] == "agent.run.failed"
    assert events[-1]["payload"]["error"]["code"] == "INTENT_CAPABILITY_UNAVAILABLE"


def test_ambiguous_delete_is_deterministically_clarified_without_effect_or_permission():
    captured = []
    model = _HarnessModel(
        [
            _intent_output(
                workspace_evidence="metadata",
                workspace_action="destructive",
                workspace_ambiguity="clarification_required",
            )
        ],
        [AgentAction.call_tool("workspace.delete_path", {"path": "old"}, "删除旧内容")],
    )
    runner = AgentRunner(
        model_provider=model,
        tool_gateway=_workspace_gateway(captured),
        intent_extractor=LlmIntentExtractor(model),
        intent_context_provider=_ContextProvider(IntentRuntimeContext()),
        max_iterations=3,
    )

    events = [item.runtime_event for item in runner.run(_job("删除 incoming 里所有重复文件。"))]

    assert captured == []
    assert not any(event["type"] == "permission.required" for event in events)
    assert events[-1]["type"] == "agent.run.completed"
    assert "具体相对路径" in events[-1]["payload"]["output"]
    assert "不会执行写入或删除" in events[-1]["payload"]["output"]
