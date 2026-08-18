"""OpenAI-compatible 真实 ModelProvider。

这是可复用的 OpenAI Chat Completions 协议实现：
- 供应商扩展由具体 Provider（例如 DeepSeekProvider）负责。
- finish_reason 仅接受 "stop"，其余全部拒绝。
- 删除 _safe_repr；网络错误仅记录异常类型。
- 不记录 request headers、Authorization、response body、完整 URL query。
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Literal

import httpx

from jarvis_worker.agent.context.types import ContextPackage, ModelContextProfile
from jarvis_worker.agent.core.action_parser import ParseAgentActionError, parse_agent_action
from jarvis_worker.agent.core.actions import AgentAction
from jarvis_worker.agent.core.required_tool_recovery import (
    recover_required_rag_search_action,
)
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.core.structured_output import retry_instruction_for
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
from jarvis_worker.agent.models.openai_compatible_adapter import build_request_body
from jarvis_worker.agent.models.provider import ModelProvider
from jarvis_worker.agent.models.provider_config import (
    check_api_key_exists,
    read_api_key,
    validate_provider_config,
)
from jarvis_worker.agent.models.streaming import FinalMessageStreamExtractor
from jarvis_worker.agent.prompts.builder import PromptBuilder

if TYPE_CHECKING:
    from jarvis_worker.agent.models.messages import ModelMessage

log = logging.getLogger("jarvis_worker.openai_provider")

# finish_reason 白名单
_ALLOWED_FINISH_REASONS = frozenset({"stop"})
_MAX_MODEL_RESPONSE_CHARS = 65_536


class OpenAiCompatibleModelProvider(ModelProvider):
    """OpenAI /chat/completions 兼容 ModelProvider。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_env: str,
        prompt_builder: PromptBuilder,
        timeout: float = 120.0,
        max_retries: int = 1,
        max_tokens: int = 4096,
        context_window_tokens: int = 32_768,
        provider_name: str = "custom_openai_compatible",
        _client_factory: Callable[[], httpx.Client] | None = None,
        _sleeper: Callable[[float], None] = time.sleep,
    ):
        base_url = base_url.strip().rstrip("/")
        model = model.strip()
        api_key_env = api_key_env.strip()

        validate_provider_config(base_url, model, api_key_env, max_retries)
        check_api_key_exists(api_key_env)

        self._base_url = base_url
        self._model = model
        self._api_key_env = api_key_env
        self._prompt_builder = prompt_builder
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_tokens = max_tokens
        self._context_window_tokens = context_window_tokens
        self._provider_name = provider_name
        self._client_factory = _client_factory or _default_client_factory
        self._sleeper = _sleeper

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
            history_messages=list(state.history_messages) if state.history_messages else None,
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
        messages: list["ModelMessage"],
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
                if on_text_delta is not None:
                    on_text_delta(delta)

        for attempt in range(self._max_retries + 1):
            body = self._build_request_body(
                request_messages,
                stream=on_text_delta is not None,
            )
            try:
                return self._do_streaming_request(body, safe_delta)
            except ModelProviderError as e:
                e.attempt_count = attempt + 1
                last_error = e
                # 已对用户展示的 partial output 不可安全地在重试后重复拼接。
                if streamed_text or not self._should_retry_error(e, attempt):
                    raise
                if attempt < self._max_retries:
                    wait = min(2**attempt, 4)
                    log.warning(
                        "模型请求失败 (attempt=%d/%d code=%s)，%ss 后重试",
                        attempt + 1,
                        self._max_retries + 1,
                        e.code,
                        wait,
                    )
                    request_messages = self._prepare_retry_messages(
                        messages,
                        e,
                    )
                    self._sleeper(wait)
        raise last_error  # type: ignore[misc]

    def complete_structured(
        self,
        messages: list["ModelMessage"],
        parser: Callable[[str], Any],
        *,
        protocol_mode: Literal["finish_only", "tool_required"] | None = None,
    ) -> Any:
        """Execute one bounded non-streaming structured model operation.

        Constrained Runtime phases own one protocol-correction attempt even when
        transport retries are configured to zero.  Network resilience and action
        schema recovery are separate Harness budgets.
        """
        last_error: ModelProviderError | None = None
        request_messages = list(messages)
        retry_budget = max(self._max_retries, 1 if protocol_mode else 0)
        for attempt in range(retry_budget + 1):
            body = self._build_request_body(request_messages, stream=False)
            try:
                return parser(self._do_request_content(body))
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
                    wait = min(2**attempt, 4)
                    log.warning(
                        "结构化模型请求失败 (attempt=%d/%d code=%s)，%ss 后重试",
                        attempt + 1,
                        retry_budget + 1,
                        error.code,
                        wait,
                    )
                    if protocol_mode == "finish_only":
                        request_messages = _append_finish_only_retry(messages)
                    elif protocol_mode == "tool_required":
                        request_messages = _append_tool_required_retry(messages)
                    else:
                        request_messages = self._prepare_retry_messages(messages, error)
                    self._sleeper(wait)
        raise last_error  # type: ignore[misc]

    def _should_retry_error(
        self,
        error: ModelProviderError,
        attempt: int,
    ) -> bool:
        """判断当前错误能否重试。

        通用兼容 Provider 只服从统一错误的 recoverable 语义，不假设供应商
        支持 JSON mode，也不擅自重试结构化输出失败。供应商子类可按官方契约
        覆写，但仍受 max_retries 和已输出安全文本的边界约束。
        """
        del attempt
        return error.recoverable

    def _build_request_body(
        self,
        messages: list["ModelMessage"],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """构造通用 OpenAI-compatible 请求；供应商可覆写扩展字段。"""
        return build_request_body(
            messages,
            model=self._model,
            max_tokens=self._max_tokens,
            stream=stream,
        )

    def _prepare_retry_messages(
        self,
        messages: list["ModelMessage"],
        error: ModelProviderError,
    ) -> list["ModelMessage"]:
        """构造下一次请求的消息；通用 Provider 默认保持原上下文。"""
        del error
        return list(messages)

    def _do_streaming_request(
        self,
        body: dict[str, Any],
        on_text_delta: Callable[[str], None],
    ) -> AgentAction:
        api_key = read_api_key(self._api_key_env)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        url = f"{self._base_url}/chat/completions"
        extractor = FinalMessageStreamExtractor()
        content_parts: list[str] = []
        finish_reason: str | None = None
        received_done = False

        try:
            with self._client_factory() as client:
                with client.stream(
                    "POST", url, json=body, headers=headers, timeout=self._timeout
                ) as resp:
                    if resp.status_code != 200:
                        raise model_http_error(resp.status_code, f"HTTP {resp.status_code}")
                    for raw_line in resp.iter_lines():
                        line = raw_line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_line = line[5:].strip()
                        if data_line == "[DONE]":
                            received_done = True
                            break
                        chunk = _parse_stream_chunk(data_line)
                        choice = chunk["choices"][0]
                        raw_finish = choice.get("finish_reason")
                        if raw_finish is not None:
                            finish_reason = raw_finish
                        delta = choice.get("delta", {})
                        content = delta.get("content") if isinstance(delta, dict) else None
                        if content is None:
                            continue
                        if not isinstance(content, str):
                            raise model_response_invalid("stream delta.content 不是字符串")
                        if (
                            sum(len(part) for part in content_parts) + len(content)
                            > _MAX_MODEL_RESPONSE_CHARS
                        ):
                            raise model_output_invalid(
                                "模型响应超过最大长度",
                                failure_kind="response_too_large",
                            )
                        content_parts.append(content)
                        for safe_text in extractor.feed(content):
                            on_text_delta(safe_text)
        except httpx.TimeoutException:
            raise model_timeout_error(f"请求超时 ({self._timeout}s)") from None
        except httpx.RequestError:
            raise model_http_error(0, "网络请求失败") from None

        if not received_done:
            raise model_response_invalid("stream 响应缺少 [DONE]")
        if finish_reason not in _ALLOWED_FINISH_REASONS:
            if finish_reason is None:
                raise model_output_invalid(
                    "stream 响应缺少 finish_reason",
                    failure_kind="missing_finish_reason",
                )
            raise _finish_reason_error(finish_reason)
        return self._parse_action_content("".join(content_parts))

    def _do_request_content(self, body: dict[str, Any]) -> str:
        api_key = read_api_key(self._api_key_env)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        url = f"{self._base_url}/chat/completions"

        try:
            with self._client_factory() as client:
                resp = client.post(url, json=body, headers=headers, timeout=self._timeout)
        except httpx.TimeoutException:
            raise model_timeout_error(f"请求超时 ({self._timeout}s)") from None
        except httpx.RequestError:
            raise model_http_error(0, "网络请求失败") from None

        if resp.status_code != 200:
            raise model_http_error(resp.status_code, f"HTTP {resp.status_code}")

        try:
            data = resp.json()
        except Exception:
            raise model_response_invalid("响应体不是合法 JSON")

        if not isinstance(data, dict):
            raise model_response_invalid("响应体不是 JSON object")

        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) == 0:
            raise model_response_invalid("响应缺少 choices 或为空")

        choice = choices[0]
        if not isinstance(choice, dict):
            raise model_response_invalid("choice 不是 JSON object")

        finish = choice.get("finish_reason", "")
        if finish not in _ALLOWED_FINISH_REASONS:
            if finish == "":
                raise model_output_invalid(
                    "响应缺少 finish_reason",
                    failure_kind="missing_finish_reason",
                )
            raise _finish_reason_error(finish)

        message = choice.get("message")
        if not isinstance(message, dict):
            raise model_response_invalid("choice.message 缺失或不是 object")

        if message.get("tool_calls"):
            raise model_output_invalid(
                "模型返回了意外的 tool_calls，本轮不支持供应商原生工具调用",
                failure_kind="unexpected_tool_calls",
            )

        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
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
            allowed = self._prompt_builder.allowed_tool_names
            return parse_agent_action(content, allowed_tools=allowed)
        except ParseAgentActionError as e:
            raise model_output_invalid(
                f"模型输出解析失败: {e}",
                failure_kind=e.failure_kind,
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
        return (
            f"OpenAiCompatibleModelProvider("
            f"base_url={self._base_url!r}, model={self._model!r}, "
            f"timeout={self._timeout})"
        )


# ================================================================
# 内部工具
# ================================================================


def _default_client_factory() -> httpx.Client:
    return httpx.Client()


def _parse_stream_chunk(data_line: str) -> dict[str, Any]:
    """校验单个 SSE data frame，不暴露其原文。"""
    try:
        data = json.loads(data_line)
    except (json.JSONDecodeError, TypeError):
        raise model_response_invalid("stream data 不是合法 JSON")
    if not isinstance(data, dict):
        raise model_response_invalid("stream data 不是 JSON object")
    choices = data.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise model_response_invalid("stream data 缺少有效 choices")
    return data


def _finish_reason_error(finish_reason: object) -> ModelProviderError:
    kind = "truncated_output" if finish_reason == "length" else "unexpected_finish_reason"
    return model_output_invalid(
        "finish_reason 未预期",
        failure_kind=kind,
    )


def _append_structured_output_retry(
    messages: list["ModelMessage"],
    *,
    failure_kind: str,
) -> list["ModelMessage"]:
    """向可信 system 消息加入固定纠正指令，不回灌模型原始输出。"""
    from jarvis_worker.agent.models.messages import ModelMessage

    safe_kind, safe_instruction = retry_instruction_for(failure_kind)
    suffix = (
        "\n\n上一次输出未通过结构化校验。"
        f"失败类型：{safe_kind}。{safe_instruction}"
        "请重新完成原任务，只返回纠正后的 JSON object；不要复述错误或上一次输出。"
    )
    result: list[ModelMessage] = []
    injected = False
    for message in messages:
        if message.role == "system" and not injected:
            result.append(ModelMessage.system(message.content + suffix))
            injected = True
        else:
            result.append(message)
    if not injected:
        result.insert(0, ModelMessage.system(suffix.strip()))
    return result


def _append_finish_only_retry(messages: list["ModelMessage"]) -> list["ModelMessage"]:
    """为终态收口追加不含工具协议的固定纠正指令。"""
    from jarvis_worker.agent.models.messages import ModelMessage

    suffix = (
        "\n\n上一次输出不符合终态契约。不要复述错误或上一次输出。"
        "只根据已有证据返回一个 action_type 为 finish 的 JSON object；"
        "必须包含非空 final_message、citations 数组和 insufficient_evidence boolean。"
        'citations 是 RAG 专用字段，每项必须且只能是 {"chunk_id":"非空字符串"}；'
        "源码文件路径、行号和 Workspace 搜索结果必须写入 final_message，不能写入 citations；"
        "没有成功 rag.search 返回的 chunk_id 时 citations 必须是 []。"
        "不得请求任何新动作。"
    )
    result: list[ModelMessage] = []
    injected = False
    for message in messages:
        if message.role == "system" and not injected:
            result.append(ModelMessage.system(message.content + suffix))
            injected = True
        else:
            result.append(message)
    if not injected:
        result.insert(0, ModelMessage.system(suffix.strip()))
    return result


def _append_tool_required_retry(messages: list["ModelMessage"]) -> list["ModelMessage"]:
    """为工具补证模式追加固定纠正指令，不回灌模型原始输出。"""
    from jarvis_worker.agent.models.messages import ModelMessage

    suffix = (
        "\n\n上一次输出不符合工具补证契约。不要复述错误或上一次输出。"
        "当前唯一合法 action 是 call_tool；请从系统列出的已启用工具中自主选择一个，"
        "返回 tool_name、arguments 和可选 reason。不得返回 finish、Markdown、解释文字或多个对象。"
    )
    result: list[ModelMessage] = []
    injected = False
    for message in messages:
        if message.role == "system" and not injected:
            result.append(ModelMessage.system(message.content + suffix))
            injected = True
        else:
            result.append(message)
    if not injected:
        result.insert(0, ModelMessage.system(suffix.strip()))
    return result
