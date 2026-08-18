"""P6-2 LangChain ModelProvider adapter 契约测试。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import cast

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_deepseek import ChatDeepSeek

from jarvis_worker.agent.context.manager import ContextManager
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.models.errors import ModelProviderError
from jarvis_worker.agent.models.langchain_factory import OpenAiCompatibleChatModel
from jarvis_worker.agent.models.langchain_provider import LangChainModelProvider
from jarvis_worker.agent.models.messages import ModelMessage
from jarvis_worker.agent.prompts.builder import PromptBuilder


def _finish_json(message: str = "完成") -> str:
    return json.dumps(
        {"action_type": "finish", "final_message": message},
        ensure_ascii=False,
    )


def _response(content: str, *, finish_reason: str = "stop") -> AIMessage:
    return AIMessage(
        content=content,
        response_metadata={"finish_reason": finish_reason},
    )


class _StubChatModel:
    def __init__(
        self,
        *,
        responses: list[AIMessage | Exception] | None = None,
        stream_batches: list[list[AIMessageChunk] | Exception] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.stream_batches = list(stream_batches or [])
        self.invocations: list[list[BaseMessage]] = []
        self.stream_invocations: list[list[BaseMessage]] = []

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.invocations.append(messages)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def stream(self, messages: list[BaseMessage]) -> Iterable[AIMessageChunk]:
        self.stream_invocations.append(messages)
        result = self.stream_batches.pop(0)
        if isinstance(result, Exception):
            raise result
        return iter(result)


def _provider(
    model: _StubChatModel,
    *,
    retries: int = 1,
    retry_structured_output_once: bool = False,
) -> LangChainModelProvider:
    return LangChainModelProvider(
        chat_model=cast(BaseChatModel, model),
        provider_name="deepseek",
        model="test-model",
        prompt_builder=PromptBuilder(),
        max_retries=retries,
        max_tokens=100,
        retry_structured_output_once=retry_structured_output_once,
        sleeper=lambda _: None,
    )


class TestInvoke:
    def test_decide_next_action_parses_project_action(self):
        model = _StubChatModel(responses=[_response(_finish_json("已完成"))])

        action = _provider(model).decide_next_action(
            AgentState(task_id="t1", run_id="r1", user_goal="处理任务")
        )

        assert action.final_message == "已完成"
        assert model.invocations[0][0].type == "system"
        assert model.invocations[0][1].type == "human"

    def test_tool_history_remains_untrusted_human_data(self):
        model = _StubChatModel(responses=[_response(_finish_json())])
        messages = [
            ModelMessage.system("rules"),
            ModelMessage.assistant(
                json.dumps(
                    {
                        "action_type": "call_tool",
                        "tool_name": "workspace.read_file",
                        "arguments": {"path": "a.md"},
                    }
                ),
                name="workspace.read_file",
                tool_call_id="call-1",
            ),
            ModelMessage.tool(
                json.dumps({"ok": True, "data": {"content": "external"}}),
                name="workspace.read_file",
                tool_call_id="call-1",
            ),
        ]

        result = _provider(model).complete_structured(messages, json.loads)

        assert result["action_type"] == "finish"
        assert isinstance(model.invocations[0][-1], HumanMessage)
        assert "Runtime ToolResult" in str(model.invocations[0][-1].content)

    def test_native_tool_call_is_rejected(self):
        model = _StubChatModel(
            responses=[
                AIMessage(
                    content="{}",
                    tool_calls=[
                        {
                            "name": "workspace.read_file",
                            "args": {"path": "a.md"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                    response_metadata={"finish_reason": "stop"},
                )
            ]
        )

        with pytest.raises(ModelProviderError) as exc:
            _provider(model, retries=0).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="读取")
            )

        assert exc.value.output_failure_kind == "unexpected_tool_calls"

    @pytest.mark.parametrize(
        ("finish_reason", "failure_kind"),
        [(None, "missing_finish_reason"), ("length", "truncated_output")],
    )
    def test_finish_reason_is_fail_closed(self, finish_reason, failure_kind):
        response = AIMessage(content=_finish_json())
        if finish_reason is not None:
            response.response_metadata["finish_reason"] = finish_reason
        model = _StubChatModel(responses=[response])

        with pytest.raises(ModelProviderError) as exc:
            _provider(model, retries=0).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="任务")
            )

        assert exc.value.output_failure_kind == failure_kind

    def test_deepseek_structured_failure_retries_with_safe_instruction(self):
        model = _StubChatModel(
            responses=[
                _response('{"secret_marker":"DO_NOT_ECHO"}'),
                _response(_finish_json("已纠正")),
            ]
        )

        action = _provider(
            model,
            retry_structured_output_once=True,
        ).decide_next_action(
            AgentState(task_id="t1", run_id="r1", user_goal="任务")
        )

        assert action.final_message == "已纠正"
        assert len(model.invocations) == 2
        retry_system = str(model.invocations[1][0].content)
        assert "失败类型：missing_field" in retry_system
        assert "DO_NOT_ECHO" not in retry_system

    def test_finish_only_context_rejects_tool_action_and_retries_to_finish(self):
        model = _StubChatModel(responses=[
            _response(json.dumps({
                "action_type": "call_tool",
                "tool_name": "workspace.read_file",
                "arguments": {"path": "more.py"},
            })),
            _response(_finish_json("根据已有证据完成收口")),
        ])
        provider = _provider(
            model,
            retries=0,
            retry_structured_output_once=False,
        )
        state = AgentState(task_id="t1", run_id="r1", user_goal="解释调用链")
        context = ContextManager(PromptBuilder()).prepare(
            state,
            provider.context_profile,
            finish_only=True,
        )

        action = provider.decide_prepared_context_finish_only(state, context)

        assert action.action_type == "finish"
        assert action.final_message == "根据已有证据完成收口"
        assert len(model.invocations) == 2
        assert "终态收口模式" in str(model.invocations[0][0].content)
        assert "workspace.read_file" not in str(model.invocations[0][0].content)
        retry_system = str(model.invocations[1][0].content)
        assert "只根据已有证据返回一个 action_type 为 finish" in retry_system
        assert "当前允许的工具名称" not in retry_system

    def test_tool_required_context_rejects_finish_and_retries_to_tool(self):
        model = _StubChatModel(responses=[
            _response(_finish_json("证据还没齐但尝试结束")),
            _response(json.dumps({
                "action_type": "call_tool",
                "tool_name": "workspace.read_file",
                "arguments": {"path": "apps/worker.py"},
                "reason": "补充执行端调用点",
            })),
        ])
        provider = _provider(
            model,
            retries=0,
            retry_structured_output_once=False,
        )
        state = AgentState(task_id="t1", run_id="r1", user_goal="解释调用链")
        context = ContextManager(PromptBuilder()).prepare(
            state,
            provider.context_profile,
            tool_required=True,
        )

        action = provider.decide_prepared_context_tool_required(state, context)

        assert action.action_type == "call_tool"
        assert action.tool_name == "workspace.read_file"
        assert action.arguments == {"path": "apps/worker.py"}
        assert len(model.invocations) == 2
        assert "工具补证模式" in str(model.invocations[0][0].content)
        retry_system = str(model.invocations[1][0].content)
        assert "当前唯一合法 action 是 call_tool" in retry_system
        assert "证据还没齐但尝试结束" not in retry_system

    def test_finish_only_retry_keeps_workspace_paths_out_of_rag_citations(self):
        model = _StubChatModel(responses=[
            _response(json.dumps({
                "action_type": "finish",
                "final_message": "源码证据已确认",
                "citations": [{"path": "apps/gateway/internal/runtime/service.go"}],
                "insufficient_evidence": False,
            })),
            _response(json.dumps({
                "action_type": "finish",
                "final_message": (
                    "源码证据见 `apps/gateway/internal/runtime/service.go`。"
                ),
                "citations": [],
                "insufficient_evidence": False,
            })),
        ])
        provider = _provider(model, retry_structured_output_once=True)
        state = AgentState(task_id="t1", run_id="r1", user_goal="解释源码调用链")
        context = ContextManager(PromptBuilder()).prepare(
            state,
            provider.context_profile,
            finish_only=True,
        )

        action = provider.decide_prepared_context_finish_only(state, context)

        assert action.final_message == (
            "源码证据见 `apps/gateway/internal/runtime/service.go`。"
        )
        assert action.citations == ()
        assert len(model.invocations) == 2
        retry_system = str(model.invocations[1][0].content)
        assert "citations 是 RAG 专用字段" in retry_system
        assert "源码文件路径、行号和 Workspace 搜索结果必须写入 final_message" in retry_system
        assert "没有成功 rag.search 返回的 chunk_id 时 citations 必须是 []" in retry_system


class TestStreaming:
    def test_only_finish_text_is_streamed(self):
        raw = _finish_json("流式完成")
        model = _StubChatModel(
            stream_batches=[
                [
                    AIMessageChunk(content=raw[:24]),
                    AIMessageChunk(
                        content=raw[24:],
                        response_metadata={"finish_reason": "stop"},
                    ),
                ]
            ]
        )
        deltas: list[str] = []

        action = _provider(model).decide_next_action_stream(
            AgentState(task_id="t1", run_id="r1", user_goal="任务"),
            deltas.append,
        )

        assert action.final_message == "流式完成"
        assert "".join(deltas) == "流式完成"
        assert "action_type" not in "".join(deltas)

    def test_no_retry_after_safe_text_was_emitted(self):
        invalid = _finish_json("已经显示") + " trailing"
        model = _StubChatModel(
            stream_batches=[
                [
                    AIMessageChunk(
                        content=invalid,
                        response_metadata={"finish_reason": "stop"},
                    )
                ],
                [
                    AIMessageChunk(
                        content=_finish_json("不应重试"),
                        response_metadata={"finish_reason": "stop"},
                    )
                ],
            ]
        )
        deltas: list[str] = []

        with pytest.raises(ModelProviderError):
            _provider(
                model,
                retry_structured_output_once=True,
            ).decide_next_action_stream(
                AgentState(task_id="t1", run_id="r1", user_goal="任务"),
                deltas.append,
            )

        assert "".join(deltas) == "已经显示"
        assert len(model.stream_invocations) == 1


class TestErrorMapping:
    def test_third_party_error_text_and_secret_are_not_chained(self):
        model = _StubChatModel(
            responses=[RuntimeError("Authorization: Bearer sk-secret")]
        )

        with pytest.raises(ModelProviderError) as exc:
            _provider(model, retries=0).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="任务")
            )

        assert exc.value.code == "MODEL_PROVIDER_ERROR"
        assert "sk-secret" not in str(exc.value)
        assert exc.value.__cause__ is None

    def test_http_status_is_retried_by_project_budget(self):
        class RateLimitError(Exception):
            status_code = 429

        model = _StubChatModel(
            responses=[RateLimitError("secret"), _response(_finish_json("恢复"))]
        )

        action = _provider(model, retries=1).decide_next_action(
            AgentState(task_id="t1", run_id="r1", user_goal="任务")
        )

        assert action.final_message == "恢复"
        assert len(model.invocations) == 2


class TestProviderRequestParity:
    def test_deepseek_keeps_json_and_thinking_fields(self):
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return _openai_response(_finish_json())

        client = httpx.Client(transport=httpx.MockTransport(handler))
        model = ChatDeepSeek(
            model="deepseek-chat",
            api_key="test",
            base_url="https://api.deepseek.com",
            http_client=client,
            max_retries=0,
            max_tokens=77,
            model_kwargs={"response_format": {"type": "json_object"}},
            extra_body={"thinking": {"type": "disabled"}},
            stream_usage=False,
        )

        model.invoke([HumanMessage(content="hello")])

        assert captured[0]["max_tokens"] == 77
        assert captured[0]["response_format"] == {"type": "json_object"}
        assert captured[0]["thinking"] == {"type": "disabled"}

    def test_custom_compatible_keeps_legacy_max_tokens_field(self):
        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return _openai_response(_finish_json())

        client = httpx.Client(transport=httpx.MockTransport(handler))
        model = OpenAiCompatibleChatModel(
            model="custom-model",
            api_key="test",
            base_url="https://api.example.com/v1",
            http_client=client,
            max_retries=0,
            max_tokens=88,
            stream_usage=False,
            use_responses_api=False,
        )

        model.invoke([HumanMessage(content="hello")])

        assert captured[0]["max_tokens"] == 88
        assert "max_completion_tokens" not in captured[0]


def _openai_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "response-1",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        },
    )
