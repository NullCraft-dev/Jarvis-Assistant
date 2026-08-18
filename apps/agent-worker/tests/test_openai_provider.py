"""测试 models/openai_compatible_provider.py。全量 httpx.MockTransport。"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from jarvis_worker.agent.context.manager import ContextManager
from jarvis_worker.agent.core.state import AgentState
from jarvis_worker.agent.loop.contracts import CompletionContract
from jarvis_worker.agent.models.deepseek_provider import DeepSeekModelProvider
from jarvis_worker.agent.models.errors import ModelProviderError
from jarvis_worker.agent.models.messages import ModelMessage
from jarvis_worker.agent.models.openai_compatible_provider import (
    OpenAiCompatibleModelProvider,
)
from jarvis_worker.agent.models.streaming import FinalMessageStreamExtractor
from jarvis_worker.agent.prompts.builder import PromptBuilder


def _make_provider(
    *,
    base_url="https://api.example.com/v1",
    model="test-model",
    api_key_env="TEST_KEY",
    max_retries=1,
    max_tokens=100,
    transport=None,
    sleeper=None,
):
    cf = (lambda: httpx.Client(transport=transport)) if transport else None
    return OpenAiCompatibleModelProvider(
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        prompt_builder=PromptBuilder(),
        timeout=5.0,
        max_retries=max_retries,
        max_tokens=max_tokens,
        _client_factory=cf,
        _sleeper=sleeper or (lambda s: None),
    )


def _make_deepseek_provider(*, thinking_mode="", transport=None, max_retries=1):
    cf = (lambda: httpx.Client(transport=transport)) if transport else None
    return DeepSeekModelProvider(
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        api_key_env="TEST_KEY",
        prompt_builder=PromptBuilder(),
        timeout=5.0,
        max_retries=max_retries,
        max_tokens=100,
        thinking_mode=thinking_mode,
        _client_factory=cf,
        _sleeper=lambda _: None,
    )


def _ok_resp(content: str) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ]
            },
        )
    )


def _finish_json(msg="done"):
    return json.dumps({"action_type": "finish", "final_message": msg})


def _sse_response(*content_parts: str, finish_reason: str = "stop") -> httpx.Response:
    frames: list[str] = []
    for content in content_parts:
        frames.append(
            "data: "
            + json.dumps({"choices": [{"delta": {"content": content}, "finish_reason": None}]})
            + "\n\n"
        )
    frames.append(
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish_reason}]}) + "\n\n"
    )
    frames.append("data: [DONE]\n\n")
    return httpx.Response(200, content="".join(frames).encode())


# ============================================================
# 成功
# ============================================================


class TestSuccess:
    def test_generic_structured_completion_uses_caller_parser(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        provider = _make_provider(transport=_ok_resp('{"kind":"intent","confidence":0.9}'))

        result = provider.complete_structured(
            [ModelMessage.system("return json"), ModelMessage.user("classify")],
            json.loads,
        )

        assert result == {"kind": "intent", "confidence": 0.9}

    def test_finish(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        p = _make_provider(transport=_ok_resp(_finish_json("完成")))
        a = p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))
        assert a.action_type == "finish"

    def test_call_tool(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        p = _make_provider(
            transport=_ok_resp(
                json.dumps(
                    {
                        "action_type": "call_tool",
                        "tool_name": "workspace.list_files",
                        "arguments": {"path": "."},
                    }
                )
            )
        )
        a = p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))
        assert a.action_type == "call_tool"

    def test_request_format(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        cap: list[dict] = []
        p = _make_provider(
            transport=httpx.MockTransport(
                lambda r: (
                    cap.append(json.loads(r.content)),
                    httpx.Response(
                        200,
                        json={
                            "choices": [
                                {"message": {"content": _finish_json()}, "finish_reason": "stop"}
                            ]
                        },
                    ),
                )[1]
            )
        )
        p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))
        b = cap[0]
        assert b["stream"] is False
        assert "response_format" not in b

    def test_effect_guard_feedback_is_sent_as_trusted_system_context(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        captured: list[dict] = []
        provider = _make_provider(
            transport=httpx.MockTransport(
                lambda request: (
                    captured.append(json.loads(request.content)),
                    httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {"content": _finish_json()},
                                    "finish_reason": "stop",
                                }
                            ],
                        },
                    ),
                )[1]
            )
        )
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="创建文件",
            effect_guard_feedback=("上一次 finish 被拒绝，必须调用 workspace.create_file。"),
        )

        provider.decide_next_action(state)

        system_message = captured[0]["messages"][0]
        assert system_message["role"] == "system"
        assert "Runtime 校验反馈（可信系统状态）" in system_message["content"]
        assert "必须调用 workspace.create_file" in system_message["content"]

    def test_history_assistant_uses_agent_action_json_on_wire(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        cap: list[dict] = []
        p = _make_provider(
            transport=httpx.MockTransport(
                lambda r: (
                    cap.append(json.loads(r.content)),
                    httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {"content": _finish_json("JARVIS-715")},
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                    ),
                )[1]
            )
        )
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="代号是什么",
            history_messages=[
                {"role": "user", "content": "记住代号"},
                {"role": "assistant", "content": "已记住"},
            ],
        )

        p.decide_next_action(state)

        assistant = [m for m in cap[0]["messages"] if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert json.loads(assistant[0]["content"]) == {
            "action_type": "finish",
            "final_message": "已记住",
        }

    def test_stream_finish_exposes_only_final_message_deltas(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        raw = json.dumps(
            {"action_type": "finish", "final_message": "第一行\n第二行"},
            ensure_ascii=False,
        )
        captured_body: list[dict] = []
        provider = _make_provider(
            transport=httpx.MockTransport(
                lambda request: (
                    captured_body.append(json.loads(request.content)),
                    _sse_response(raw[:23], raw[23:41], raw[41:]),
                )[1]
            )
        )
        deltas: list[str] = []

        action = provider.decide_next_action_stream(
            AgentState(task_id="t1", run_id="r1", user_goal="hi"),
            deltas.append,
        )

        assert action.action_type == "finish"
        assert "".join(deltas) == "第一行\n第二行"
        assert all("action_type" not in delta for delta in deltas)
        assert captured_body[0]["stream"] is True

    def test_stream_tool_action_never_emits_arguments(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        raw = json.dumps(
            {
                "action_type": "call_tool",
                "tool_name": "workspace.list_files",
                "arguments": {"path": "sensitive-path"},
            }
        )
        provider = _make_provider(
            transport=httpx.MockTransport(lambda _request: _sse_response(raw[:30], raw[30:]))
        )
        deltas: list[str] = []

        action = provider.decide_next_action_stream(
            AgentState(task_id="t1", run_id="r1", user_goal="hi"),
            deltas.append,
        )

        assert action.action_type == "call_tool"
        assert deltas == []


class TestFinalMessageStreamExtractor:
    def test_handles_split_escapes_and_surrogate_pair(self):
        extractor = FinalMessageStreamExtractor()
        raw = '{"action_type":"finish","final_message":"A\\n\\uD83D\\uDE80"}'
        output: list[str] = []
        for fragment in (raw[:36], raw[36:43], raw[43:49], raw[49:]):
            output.extend(extractor.feed(fragment))
        assert "".join(output) == "A\n🚀"


# ============================================================
# finish_reason 严格校验
# ============================================================


class TestFinishReason:
    def test_stop_accepted(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        p = _make_provider(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"content": _finish_json()}, "finish_reason": "stop"}
                        ]
                    },
                )
            )
        )
        p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))

    @pytest.mark.parametrize(
        "reason",
        ["length", "content_filter", "tool_calls", "insufficient_system_resource", "", "unknown"],
    )
    def test_non_stop_rejected(self, reason, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        t = httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": reason}]}
            )
        )
        p = _make_provider(transport=t)
        with pytest.raises(ModelProviderError, match="finish_reason"):
            p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))


# ============================================================
# 错误
# ============================================================


class TestErrors:
    def test_empty_content(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        p = _make_provider(transport=_ok_resp(""))
        with pytest.raises(ModelProviderError, match="空 content"):
            p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))

    def test_non_json(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        p = _make_provider(transport=_ok_resp("not json"))
        with pytest.raises(ModelProviderError, match="解析失败"):
            p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))

    def test_unexpected_tool_calls(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        t = httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "{}", "tool_calls": [{"id": "x"}]},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        )
        with pytest.raises(ModelProviderError, match="tool_calls"):
            _make_provider(transport=t).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )

    def test_non_dict_body(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        t = httpx.MockTransport(lambda r: httpx.Response(200, content=b"string"))
        with pytest.raises(ModelProviderError, match="合法 JSON"):
            _make_provider(transport=t).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )


# ============================================================
# HTTP / 重试
# ============================================================


class TestHttpRetry:
    def test_401_no_retry(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        cnt = [0]
        t = httpx.MockTransport(
            lambda r: (cnt.__setitem__(0, cnt[0] + 1), httpx.Response(401, json={}))[1]
        )
        with pytest.raises(ModelProviderError, match="HTTP 401"):
            _make_provider(transport=t).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )
        assert cnt[0] == 1

    def test_429_retries(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        cnt = [0]

        def h(r):
            cnt[0] += 1
            if cnt[0] < 3:
                return httpx.Response(429, json={})
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": _finish_json()}, "finish_reason": "stop"}]
                },
            )

        p = _make_provider(transport=httpx.MockTransport(h), max_retries=2)
        assert (
            p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi")).action_type
            == "finish"
        )
        assert cnt[0] == 3

    def test_500_exhausts_then_fails(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        cnt = [0]
        t = httpx.MockTransport(
            lambda r: (cnt.__setitem__(0, cnt[0] + 1), httpx.Response(500, json={}))[1]
        )
        with pytest.raises(ModelProviderError, match="HTTP 500"):
            _make_provider(transport=t, max_retries=1).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )
        assert cnt[0] == 2

    def test_timeout_retries(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        cnt = [0]
        t = httpx.MockTransport(
            lambda r: (
                cnt.__setitem__(0, cnt[0] + 1),
                (_ for _ in ()).throw(httpx.TimeoutException("t")),
            )[1]
        )
        with pytest.raises(ModelProviderError):
            _make_provider(transport=t, max_retries=2).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )
        assert cnt[0] == 3


# ============================================================
# 密钥安全
# ============================================================


class TestExceptionChain:
    def test_timeout_from_none(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        t = httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.TimeoutException("timeout with secret sk-abc"))
        )
        try:
            _make_provider(transport=t, max_retries=0).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )
        except ModelProviderError as e:
            assert e.__cause__ is None  # from None
            assert "sk-abc" not in str(e)

    def test_request_error_from_none(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        t = httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.RequestError("auth: sk-secret"))
        )
        try:
            _make_provider(transport=t, max_retries=0).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )
        except ModelProviderError as e:
            assert e.__cause__ is None
            assert "sk-secret" not in str(e)

    def test_secret_not_in_traceback(self, monkeypatch):
        import traceback

        monkeypatch.setenv("TEST_KEY", "sk-test")
        t = httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.TimeoutException("secret-sk-123"))
        )
        try:
            _make_provider(transport=t, max_retries=0).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )
        except ModelProviderError:
            tb = traceback.format_exc()
            assert "secret-sk-123" not in tb

    def test_secret_not_in_caplog(self, monkeypatch, caplog):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        t = httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.RequestError("auth: secret-sk-xyz"))
        )
        with caplog.at_level(logging.WARNING, logger="jarvis_worker.openai_provider"):
            try:
                _make_provider(transport=t, max_retries=1).decide_next_action(
                    AgentState(task_id="t1", run_id="r1", user_goal="hi")
                )
            except ModelProviderError:
                pass
        assert "secret-sk-xyz" not in caplog.text


class TestConfigNormalization:
    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-test")
        p = _make_provider(
            base_url="  https://api.example.com/v1  ",
            model="  test-model  ",
            api_key_env="  MY_KEY  ",
        )
        assert p._base_url == "https://api.example.com/v1"
        assert p._model == "test-model"
        assert p._api_key_env == "MY_KEY"

    def test_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("K", "sk-test")
        p = _make_provider(base_url="https://api.example.com/v1/  ", api_key_env="  K  ")
        assert p._base_url == "https://api.example.com/v1"
        assert p._api_key_env == "K"

    def test_repr_no_whitespace_keys(self, monkeypatch):
        monkeypatch.setenv("K", "sk-test")
        p = _make_provider(base_url=" https://api.example.com/v1 ", api_key_env=" K ")
        r = repr(p)
        assert "sk-test" not in r
        assert "  " not in r.split("base_url=")[1].split(",")[0]


class TestUrlValidation:
    def test_no_hostname_rejected(self):
        with pytest.raises(ModelProviderError, match="hostname"):
            _make_provider(base_url="https:///v1")

    def test_invalid_key_env_name_rejected(self):
        with pytest.raises(ModelProviderError, match="环境变量名"):
            _make_provider(api_key_env="123bad")


class TestKeySafety:
    def test_repr_no_key(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-secret-abc")
        r = repr(_make_provider())
        assert "sk-secret-abc" not in r

    def test_error_no_key_in_message(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-secret-abc")
        t = httpx.MockTransport(lambda r: httpx.Response(500, json={}))
        try:
            _make_provider(transport=t, max_retries=0).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )
        except ModelProviderError as e:
            assert "sk-secret-abc" not in str(e)

    def test_network_error_no_key(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-secret-abc")
        t = httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.RequestError("auth: sk-secret-abc"))
        )
        try:
            _make_provider(transport=t, max_retries=0).decide_next_action(
                AgentState(task_id="t1", run_id="r1", user_goal="hi")
            )
        except ModelProviderError as e:
            assert "sk-secret-abc" not in str(e)

    def test_caplog_no_key(self, monkeypatch, caplog):
        monkeypatch.setenv("TEST_KEY", "sk-secret-abc")
        t = httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.RequestError("auth: sk-secret-abc"))
        )
        with caplog.at_level(logging.WARNING, logger="jarvis_worker.openai_provider"):
            try:
                _make_provider(transport=t, max_retries=1).decide_next_action(
                    AgentState(task_id="t1", run_id="r1", user_goal="hi")
                )
            except ModelProviderError:
                pass
        log_text = caplog.text
        assert "sk-secret-abc" not in log_text


# ============================================================
# thinking_mode
# ============================================================


class TestThinkingMode:
    def test_disabled_in_body(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        cap: list[dict] = []
        p = _make_deepseek_provider(
            thinking_mode="disabled",
            transport=httpx.MockTransport(
                lambda r: (
                    cap.append(json.loads(r.content)),
                    httpx.Response(
                        200,
                        json={
                            "choices": [
                                {"message": {"content": _finish_json()}, "finish_reason": "stop"}
                            ]
                        },
                    ),
                )[1]
            ),
        )
        p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))
        assert cap[0].get("thinking") == {"type": "disabled"}
        assert cap[0].get("response_format") == {"type": "json_object"}

    def test_empty_not_sent(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        cap: list[dict] = []
        p = _make_deepseek_provider(
            thinking_mode="",
            transport=httpx.MockTransport(
                lambda r: (
                    cap.append(json.loads(r.content)),
                    httpx.Response(
                        200,
                        json={
                            "choices": [
                                {"message": {"content": _finish_json()}, "finish_reason": "stop"}
                            ]
                        },
                    ),
                )[1]
            ),
        )
        p.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))
        assert "thinking" not in cap[0]
        assert cap[0].get("response_format") == {"type": "json_object"}

    def test_deepseek_retries_output_invalid_once(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        calls = [0]
        request_bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            request_bodies.append(json.loads(request.content))
            content = '{"secret_marker":"DO_NOT_ECHO"}' if calls[0] == 1 else _finish_json("已纠正")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": content},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        provider = _make_deepseek_provider(transport=httpx.MockTransport(handler))
        action = provider.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))

        assert action.final_message == "已纠正"
        assert calls[0] == 2
        first_system = request_bodies[0]["messages"][0]["content"]
        retry_system = request_bodies[1]["messages"][0]["content"]
        assert "上一次输出未通过结构化校验" not in first_system
        assert "失败类型：missing_field" in retry_system
        assert "DO_NOT_ECHO" not in retry_system

    def test_deepseek_tool_required_retries_finish_as_tool_action(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        request_bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_bodies.append(json.loads(request.content))
            content = (
                _finish_json("ILLEGAL_FINISH_MARKER")
                if len(request_bodies) == 1
                else json.dumps(
                    {
                        "action_type": "call_tool",
                        "tool_name": "workspace.read_file",
                        "arguments": {"path": "runtime/worker.py"},
                        "reason": "补执行端证据",
                    }
                )
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": content},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        provider = _make_deepseek_provider(
            transport=httpx.MockTransport(handler),
            max_retries=0,
        )
        state = AgentState(task_id="t1", run_id="r1", user_goal="解释调用链")
        context = ContextManager(PromptBuilder()).prepare(
            state,
            provider.context_profile,
            tool_required=True,
        )

        action = provider.decide_prepared_context_tool_required(state, context)

        assert action.action_type == "call_tool"
        assert action.arguments == {"path": "runtime/worker.py"}
        assert len(request_bodies) == 2
        retry_system = request_bodies[1]["messages"][0]["content"]
        assert "当前唯一合法 action 是 call_tool" in retry_system
        assert "ILLEGAL_FINISH_MARKER" not in retry_system

    def test_deepseek_tool_required_recovers_unique_rag_action_after_protocol_exhaustion(
        self, monkeypatch
    ):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        calls = [0]
        builder = PromptBuilder(
            allowed_tools=[
                {
                    "name": "rag.search",
                    "description": "检索 RAG 证据",
                    "parameters": {"query": "检索问题", "top_k": "结果数"},
                }
            ]
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": _finish_json("仍然拒绝调用工具")},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        provider = DeepSeekModelProvider(
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            api_key_env="TEST_KEY",
            prompt_builder=builder,
            max_retries=0,
            _client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
            _sleeper=lambda _: None,
        )
        state = AgentState(
            task_id="t1",
            run_id="r1",
            user_goal="论文没有回答的重要问题是什么？",
            intent={
                "retrieval": {
                    "mode": "required",
                    "query": "论文没有回答的重要问题",
                    "document_scope": "selected",
                    "resolved_document_ids": ["11111111-1111-4111-8111-111111111111"],
                }
            },
            completion_contract=CompletionContract(requires_rag_evidence=True).to_state_dict(),
        )
        context = ContextManager(builder).prepare(
            state,
            provider.context_profile,
            tool_required=True,
        )

        action = provider.decide_prepared_context_tool_required(state, context)

        assert calls[0] == 2
        assert action.action_type == "call_tool"
        assert action.tool_name == "rag.search"
        assert action.arguments["query"] == "论文没有回答的重要问题"

    def test_deepseek_latex_backslash_is_repaired_without_retry(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        calls = [0]
        request_bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            request_bodies.append(json.loads(request.content))
            content = (
                '{"action_type":"finish","final_message":"\\gamma"}'
                if calls[0] == 1
                else _finish_json("已纠正")
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": content},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        provider = _make_deepseek_provider(transport=httpx.MockTransport(handler))
        action = provider.decide_next_action(
            AgentState(task_id="t1", run_id="r1", user_goal="解释公式")
        )

        assert action.final_message == r"\gamma"
        assert calls[0] == 1
        first_system = request_bodies[0]["messages"][0]["content"]
        assert "上一次输出未通过结构化校验" not in first_system

    def test_deepseek_exhausted_error_has_safe_failure_metadata(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        provider = _make_deepseek_provider(transport=_ok_resp(""))

        with pytest.raises(ModelProviderError) as exc:
            provider.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))

        assert exc.value.code == "MODEL_OUTPUT_INVALID"
        assert exc.value.output_failure_kind == "empty_content"
        assert exc.value.attempt_count == 2
        assert "sk-test" not in str(exc.value)

    def test_deepseek_stream_retries_before_safe_delta(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        calls = [0]

        def handler(_request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            content = "{}" if calls[0] == 1 else _finish_json("流式已纠正")
            return _sse_response(content)

        provider = _make_deepseek_provider(transport=httpx.MockTransport(handler))
        deltas: list[str] = []
        action = provider.decide_next_action_stream(
            AgentState(task_id="t1", run_id="r1", user_goal="hi"),
            deltas.append,
        )

        assert action.final_message == "流式已纠正"
        assert "".join(deltas) == "流式已纠正"
        assert calls[0] == 2

    def test_deepseek_stream_never_retries_after_safe_delta(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        calls = [0]
        invalid = (
            json.dumps(
                {"action_type": "finish", "final_message": "已经显示"},
                ensure_ascii=False,
            )
            + " trailing"
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            return _sse_response(invalid)

        provider = _make_deepseek_provider(transport=httpx.MockTransport(handler))
        deltas: list[str] = []
        with pytest.raises(ModelProviderError) as exc:
            provider.decide_next_action_stream(
                AgentState(task_id="t1", run_id="r1", user_goal="hi"),
                deltas.append,
            )

        assert exc.value.code == "MODEL_OUTPUT_INVALID"
        assert "".join(deltas) == "已经显示"
        assert calls[0] == 1

    def test_generic_provider_does_not_retry_output_invalid(self, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        calls = [0]

        def handler(_request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "{}"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        provider = _make_provider(transport=httpx.MockTransport(handler), max_retries=1)
        with pytest.raises(ModelProviderError) as exc:
            provider.decide_next_action(AgentState(task_id="t1", run_id="r1", user_goal="hi"))

        assert exc.value.code == "MODEL_OUTPUT_INVALID"
        assert calls[0] == 1


# ============================================================
# 启动 fail-closed
# ============================================================


class TestStartupFailClosed:
    def test_missing_key_env_fails_at_init(self):
        with pytest.raises(ModelProviderError, match="未设置|NO_KEY"):
            _make_provider(api_key_env="NO_SUCH_KEY_VAR")

    def test_bad_url_no_scheme(self):
        with pytest.raises(ModelProviderError, match="http/https"):
            _make_provider(base_url="not-a-url")
