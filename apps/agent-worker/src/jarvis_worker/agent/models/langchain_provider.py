"""LangChain chat model 到项目 ModelProvider 的受控适配层。

LangChain 只拥有消息转换和模型调用。AgentAction、重试预算、安全流式输出、
ToolGateway、权限、checkpoint 与 RuntimeEvent 仍由项目 Harness 拥有。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from typing import Any, Literal

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from jarvis_worker.agent.context.types import ContextPackage, ModelContextProfile
from jarvis_worker.agent.core.action_parser import ParseAgentActionError, parse_agent_action
from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.required_tool_recovery import (
    recover_required_rag_search_action,
)
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.core.workspace_listing_projection import (
    project_workspace_listing_observations,
)
from jarvis_worker.agent.models.errors import (
    ModelProviderError,
    model_http_error,
    model_output_invalid,
    model_response_invalid,
    model_timeout_error,
)
from jarvis_worker.agent.models.messages import ModelMessage
from jarvis_worker.agent.models.openai_compatible_adapter import build_chat_messages
from jarvis_worker.agent.models.openai_compatible_provider import (
    _append_finish_only_retry,
    _append_structured_output_retry,
    _append_tool_required_retry,
)
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.models.streaming import FinalMessageStreamExtractor
from jarvis_worker.agent.prompts.builder import PromptBuilder

log = logging.getLogger("jarvis_worker.langchain_provider")

_ALLOWED_FINISH_REASONS = frozenset({"stop"})
_MAX_MODEL_RESPONSE_CHARS = 65_536


class LangChainModelProvider(ModelProvider):
    """把供应商 ChatModel 适配成 Jarvis 的稳定 ModelProvider。"""

    def __init__(
        self,
        *,
        chat_model: BaseChatModel,
        provider_name: str,
        model: str,
        prompt_builder: PromptBuilder,
        max_retries: int = 1,
        max_tokens: int = 4096,
        context_window_tokens: int = 32_768,
        retry_structured_output_once: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._chat_model = chat_model
        self._provider_name = provider_name
        self._model = model
        self._prompt_builder = prompt_builder
        self._max_retries = max_retries
        self._max_tokens = max_tokens
        self._context_window_tokens = context_window_tokens
        self._retry_structured_output_once = retry_structured_output_once
        self._sleeper = sleeper

    def decide_next_action(self, state: AgentState) -> AgentAction:
        return self._decide(state, on_text_delta=None)

    def decide_next_action_stream(
        self,
        state: AgentState,
        on_text_delta: Callable[[str], None],
    ) -> AgentAction:
        return self._decide(state, on_text_delta=on_text_delta)

    def _decide(
        self,
        state: AgentState,
        on_text_delta: Callable[[str], None] | None,
    ) -> AgentAction:
        messages = self._prompt_builder.build_messages(
            user_goal=state.user_goal,
            observations=project_workspace_listing_observations(
                list(state.observations), state.intent
            ),
            history_messages=(list(state.history_messages) if state.history_messages else None),
            runtime_feedback=(
                [
                    feedback
                    for feedback in (
                        state.effect_guard_feedback,
                        state.answer_guard_feedback,
                    )
                    if feedback
                ]
                or None
            ),
        )
        return self._decide_messages(messages, on_text_delta)

    def decide_prepared_context(
        self,
        state: AgentState,
        context: ContextPackage,
    ) -> AgentAction:
        del state
        return self._decide_messages(list(context.messages), on_text_delta=None)

    def decide_prepared_context_stream(
        self,
        state: AgentState,
        context: ContextPackage,
        on_text_delta: Callable[[str], None],
    ) -> AgentAction:
        del state
        return self._decide_messages(list(context.messages), on_text_delta)

    def decide_prepared_context_finish_only(
        self,
        state: AgentState,
        context: ContextPackage,
    ) -> AgentAction:
        del state
        return self.complete_structured(
            list(context.messages),
            self._parse_finish_only_action_content,
            protocol_mode="finish_only",
        )

    def decide_prepared_context_tool_required(
        self,
        state: AgentState,
        context: ContextPackage,
    ) -> AgentAction:
        try:
            return self.complete_structured(
                list(context.messages),
                self._parse_tool_required_action_content,
                protocol_mode="tool_required",
            )
        except ModelProviderError as error:
            recovered = (
                recover_required_rag_search_action(
                    state,
                    available_tool_names=self._prompt_builder.allowed_tool_names,
                )
                if error.code == "MODEL_OUTPUT_INVALID"
                else None
            )
            if recovered is None:
                raise
            log.warning(
                "工具补证协议连续失败，恢复已校验 Intent 唯一要求的 RAG 检索动作: attempts=%d",
                error.attempt_count,
            )
            return recovered

    def _decide_messages(
        self,
        messages: list[ModelMessage],
        on_text_delta: Callable[[str], None] | None,
    ) -> AgentAction:
        if on_text_delta is None:
            return self.complete_structured(messages, self._parse_action_content)

        last_error: ModelProviderError | None = None
        streamed_text = False
        request_messages = list(messages)

        def safe_delta(delta: str) -> None:
            nonlocal streamed_text
            if delta:
                streamed_text = True
                on_text_delta(delta)

        for attempt in range(self._max_retries + 1):
            try:
                return self._stream_action(request_messages, safe_delta)
            except ModelProviderError as error:
                error.attempt_count = attempt + 1
                last_error = error
                if streamed_text or not self._should_retry_error(error, attempt):
                    raise
                if attempt < self._max_retries:
                    self._log_retry(error, attempt, operation="流式模型请求")
                    request_messages = self._prepare_retry_messages(messages, error)
                    self._sleeper(min(2**attempt, 4))
        raise last_error  # type: ignore[misc]

    def complete_structured(
        self,
        messages: list[ModelMessage],
        parser: Callable[[str], Any],
        *,
        protocol_mode: Literal["finish_only", "tool_required"] | None = None,
    ) -> Any:
        last_error: ModelProviderError | None = None
        request_messages = list(messages)
        retry_budget = max(self._max_retries, 1 if protocol_mode else 0)
        for attempt in range(retry_budget + 1):
            try:
                response = self._invoke(request_messages)
                return parser(self._validated_response_content(response))
            except ModelProviderError as error:
                error.attempt_count = attempt + 1
                last_error = error
                protocol_retry = (
                    protocol_mode is not None
                    and error.code == "MODEL_OUTPUT_INVALID"
                    and attempt == 0
                )
                if not protocol_retry and not self._should_retry_error(error, attempt):
                    raise
                if attempt < retry_budget:
                    self._log_retry(
                        error,
                        attempt,
                        operation="结构化模型请求",
                        retry_budget=retry_budget,
                    )
                    if protocol_mode == "finish_only":
                        request_messages = _append_finish_only_retry(messages)
                    elif protocol_mode == "tool_required":
                        request_messages = _append_tool_required_retry(messages)
                    else:
                        request_messages = self._prepare_retry_messages(messages, error)
                    self._sleeper(min(2**attempt, 4))
        raise last_error  # type: ignore[misc]

    def _invoke(self, messages: list[ModelMessage]) -> AIMessage:
        try:
            response = self._chat_model.invoke(_to_langchain_messages(messages))
        except ModelProviderError:
            raise
        except Exception as error:
            raise _translate_langchain_error(error) from None
        if not isinstance(response, AIMessage):
            raise model_response_invalid("LangChain 返回值不是 AIMessage")
        return response

    def _stream_action(
        self,
        messages: list[ModelMessage],
        on_text_delta: Callable[[str], None],
    ) -> AgentAction:
        extractor = FinalMessageStreamExtractor()
        content_parts: list[str] = []
        finish_reason: object | None = None
        try:
            chunks: Iterable[BaseMessage] = self._chat_model.stream(
                _to_langchain_messages(messages)
            )
            for chunk in chunks:
                if getattr(chunk, "tool_calls", None) or getattr(chunk, "tool_call_chunks", None):
                    raise model_output_invalid(
                        "模型返回了意外的 tool_calls，本轮不支持供应商原生工具调用",
                        failure_kind="unexpected_tool_calls",
                    )
                content = _content_as_text(getattr(chunk, "content", None))
                if content:
                    if sum(map(len, content_parts)) + len(content) > _MAX_MODEL_RESPONSE_CHARS:
                        raise model_output_invalid(
                            "模型响应超过最大长度",
                            failure_kind="response_too_large",
                        )
                    content_parts.append(content)
                    for safe_text in extractor.feed(content):
                        on_text_delta(safe_text)
                metadata = getattr(chunk, "response_metadata", None)
                if isinstance(metadata, dict) and metadata.get("finish_reason") is not None:
                    finish_reason = metadata["finish_reason"]
        except ModelProviderError:
            raise
        except Exception as error:
            raise _translate_langchain_error(error) from None

        _validate_finish_reason(finish_reason)
        content = "".join(content_parts)
        if not content.strip():
            raise model_output_invalid(
                "模型返回空 content",
                failure_kind="empty_content",
            )
        return self._parse_action_content(content)

    def _validated_response_content(self, response: AIMessage) -> str:
        if response.tool_calls or response.invalid_tool_calls:
            raise model_output_invalid(
                "模型返回了意外的 tool_calls，本轮不支持供应商原生工具调用",
                failure_kind="unexpected_tool_calls",
            )
        _validate_finish_reason(response.response_metadata.get("finish_reason"))
        content = _content_as_text(response.content)
        if not content.strip():
            raise model_output_invalid(
                "模型返回空 content",
                failure_kind="empty_content",
            )
        if len(content) > _MAX_MODEL_RESPONSE_CHARS:
            raise model_output_invalid(
                "模型响应超过最大长度",
                failure_kind="response_too_large",
            )
        return content

    def _parse_action_content(self, content: str) -> AgentAction:
        try:
            return parse_agent_action(
                content,
                allowed_tools=self._prompt_builder.allowed_tool_names,
            )
        except ParseAgentActionError as error:
            raise model_output_invalid(
                f"模型输出解析失败: {error}",
                failure_kind=error.failure_kind,
            ) from None

    @staticmethod
    def _parse_finish_only_action_content(content: str) -> AgentAction:
        try:
            return parse_agent_action(
                content,
                allowed_tools=frozenset(),
                allowed_action_types=frozenset({"finish"}),
            )
        except ParseAgentActionError as error:
            raise model_output_invalid(
                f"终态收口输出解析失败: {error}",
                failure_kind=error.failure_kind,
            ) from None

    def _parse_tool_required_action_content(self, content: str) -> AgentAction:
        try:
            return parse_agent_action(
                content,
                allowed_tools=self._prompt_builder.allowed_tool_names,
                allowed_action_types=frozenset({"call_tool"}),
            )
        except ParseAgentActionError as error:
            raise model_output_invalid(
                f"工具补证输出解析失败: {error}",
                failure_kind=error.failure_kind,
            ) from None

    def _should_retry_error(self, error: ModelProviderError, attempt: int) -> bool:
        if error.recoverable:
            return True
        return (
            self._retry_structured_output_once
            and error.code == "MODEL_OUTPUT_INVALID"
            and attempt == 0
        )

    def _prepare_retry_messages(
        self,
        messages: list[ModelMessage],
        error: ModelProviderError,
    ) -> list[ModelMessage]:
        if not self._retry_structured_output_once or error.code != "MODEL_OUTPUT_INVALID":
            return list(messages)
        if any(
            message.role == "system" and "终态收口模式" in message.content for message in messages
        ):
            return _append_finish_only_retry(messages)
        if any(
            message.role == "system" and "工具补证模式" in message.content for message in messages
        ):
            return _append_tool_required_retry(messages)
        return _append_structured_output_retry(
            messages,
            failure_kind=error.output_failure_kind or "schema_violation",
        )

    def _log_retry(
        self,
        error: ModelProviderError,
        attempt: int,
        *,
        operation: str,
        retry_budget: int | None = None,
    ) -> None:
        wait = min(2**attempt, 4)
        log.warning(
            "%s失败 (attempt=%d/%d code=%s)，%ss 后重试",
            operation,
            attempt + 1,
            (self._max_retries if retry_budget is None else retry_budget) + 1,
            error.code,
            wait,
        )

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def context_profile(self) -> ModelContextProfile:
        return ModelContextProfile(
            provider=self.provider_name,
            model=self.model_name,
            context_window_tokens=self._context_window_tokens,
            max_output_tokens=self._max_tokens,
        )

    def __repr__(self) -> str:
        return f"LangChainModelProvider(provider={self._provider_name!r}, model={self._model!r})"


def _to_langchain_messages(messages: list[ModelMessage]) -> list[BaseMessage]:
    """复用既有可信 history 规则，再转换成 LangChain message。"""
    result: list[BaseMessage] = []
    for message in build_chat_messages(messages):
        role = message["role"]
        content = message["content"]
        if role == "system":
            result.append(SystemMessage(content=content))
        elif role == "user":
            result.append(HumanMessage(content=content))
        elif role == "assistant":
            result.append(AIMessage(content=content))
        else:  # build_chat_messages 默认模式只会产生以上三种角色。
            raise model_response_invalid("模型消息角色无法转换")
    return result


def _content_as_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if content == []:
        return ""
    raise model_response_invalid("LangChain 模型 content 不是纯文本")


def _validate_finish_reason(finish_reason: object | None) -> None:
    if finish_reason in _ALLOWED_FINISH_REASONS:
        return
    if finish_reason is None:
        raise model_output_invalid(
            "模型响应缺少 finish_reason",
            failure_kind="missing_finish_reason",
        )
    raise model_output_invalid(
        "finish_reason 未预期",
        failure_kind=(
            "truncated_output" if finish_reason == "length" else "unexpected_finish_reason"
        ),
    )


def _translate_langchain_error(error: Exception) -> ModelProviderError:
    """只按类型和状态映射异常，不复制第三方异常文本。"""
    if isinstance(error, httpx.TimeoutException) or "timeout" in type(error).__name__.lower():
        return model_timeout_error("模型请求超时")
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return model_http_error(status_code, f"HTTP {status_code}")
    if isinstance(error, httpx.RequestError):
        return model_http_error(0, "网络请求失败")
    return ModelProviderError(
        "MODEL_PROVIDER_ERROR",
        "模型调用失败",
        recoverable=False,
    )
