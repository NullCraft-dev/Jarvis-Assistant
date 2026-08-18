"""测试 config/settings.py 模型配置 + Phase 6 model_config projection + connectivity test。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from jarvis_worker.agent.models.deepseek_provider import DeepSeekModelProvider
from jarvis_worker.agent.models.errors import ModelProviderError
from jarvis_worker.agent.models.langchain_provider import LangChainModelProvider
from jarvis_worker.agent.prompts.builder import PromptBuilder
from jarvis_worker.bootstrap.model_factory import create_model_provider
from jarvis_worker.control_plane.model_config import (
    _validate_model_base_url,
    build_model_config,
    sanitize_base_url,
)
from jarvis_worker.shared.config.env_loader import DEFAULT_LOCAL_ENV_PATH
from jarvis_worker.shared.config.settings import (
    WorkerConfig,
    _parse_bool_strict,
    _parse_int_strict,
    _validate_model_adapter,
    _validate_model_provider,
)


class TestProvider:
    def test_default_adapter_is_langchain(self):
        assert WorkerConfig().model_adapter == "langchain"

    def test_default_deepseek(self):
        assert WorkerConfig().model_provider == "deepseek"
        assert WorkerConfig().model_context_window_tokens == 131_072

    def test_mock_is_not_a_production_provider(self):
        with pytest.raises(ValueError, match="MODEL_PROVIDER"):
            _validate_model_provider("mock")

        with pytest.raises(ModelProviderError, match="未知"):
            create_model_provider(WorkerConfig(model_provider="mock"))

    def test_openai_needs_key_env(self):
        cfg = WorkerConfig(model_provider="custom_openai_compatible", model_base_url="https://api.example.com/v1", model_name="m", model_api_key_env="NO_SUCH_KEY")
        with pytest.raises(ModelProviderError, match="未设置或为空|NO_SUCH_KEY"):
            create_model_provider(cfg, prompt_builder=PromptBuilder())

    def test_unknown_provider(self):
        with pytest.raises(ModelProviderError, match="未知"):
            create_model_provider(WorkerConfig(model_provider="unknown"))

    def test_openai_with_key(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-test")
        cfg = WorkerConfig(model_provider="custom_openai_compatible", model_base_url="https://api.example.com/v1", model_name="m", model_api_key_env="MY_KEY")
        provider = create_model_provider(cfg, prompt_builder=PromptBuilder())
        assert isinstance(provider, LangChainModelProvider)
        assert provider.provider_name == "custom_openai_compatible"

    def test_deepseek_has_dedicated_provider(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-test")
        cfg = WorkerConfig(
            model_provider="deepseek",
            model_base_url="https://api.deepseek.com",
            model_name="deepseek-chat",
            model_api_key_env="MY_KEY",
        )
        provider = create_model_provider(cfg, prompt_builder=PromptBuilder())
        assert isinstance(provider, LangChainModelProvider)
        assert provider.provider_name == "deepseek"

    def test_direct_adapter_is_explicit_fallback(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-test")
        cfg = WorkerConfig(
            model_adapter="direct",
            model_provider="deepseek",
            model_base_url="https://api.deepseek.com",
            model_name="deepseek-chat",
            model_api_key_env="MY_KEY",
        )

        provider = create_model_provider(cfg, prompt_builder=PromptBuilder())

        assert isinstance(provider, DeepSeekModelProvider)

    def test_legacy_provider_is_normalized_before_langchain_factory(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-test")
        cfg = WorkerConfig(
            model_provider="openai_compatible",
            model_base_url="https://proxy.example.com/v1",
            model_name="deepseek-chat",
            model_api_key_env="MY_KEY",
        )

        provider = create_model_provider(cfg, prompt_builder=PromptBuilder())

        assert isinstance(provider, LangChainModelProvider)
        assert provider.provider_name == "custom_openai_compatible"

    def test_adapter_from_env_can_select_direct(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_ADAPTER", "direct")
        assert WorkerConfig.from_env().model_adapter == "direct"

    def test_unknown_adapter_fails_closed(self):
        with pytest.raises(ValueError, match="MODEL_ADAPTER"):
            _validate_model_adapter("automatic")

    def test_legacy_provider_migrates_deepseek_identity(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "openai_compatible")
        monkeypatch.setenv("JARVIS_MODEL_BASE_URL", "https://api.deepseek.com")
        monkeypatch.setenv("JARVIS_MODEL_NAME", "deepseek-chat")
        cfg = WorkerConfig.from_env()
        assert cfg.model_provider == "deepseek"

    def test_legacy_proxy_endpoint_stays_custom(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "openai_compatible")
        monkeypatch.setenv("JARVIS_MODEL_BASE_URL", "https://proxy.example.com/v1")
        monkeypatch.setenv("JARVIS_MODEL_NAME", "deepseek-chat")
        cfg = WorkerConfig.from_env()
        assert cfg.model_provider == "custom_openai_compatible"

    def test_deepseek_default_base_url(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "deepseek")
        monkeypatch.delenv("JARVIS_MODEL_BASE_URL", raising=False)
        assert WorkerConfig.from_env().model_base_url == "https://api.deepseek.com"


class TestStrictInt:
    def test_valid(self, monkeypatch):
        monkeypatch.setenv("JARVIS_X", "7")
        assert _parse_int_strict("JARVIS_X", 1, min_val=1) == 7

    def test_non_int_raises(self, monkeypatch):
        monkeypatch.setenv("JARVIS_X", "abc")
        with pytest.raises(ValueError, match="整数"):
            _parse_int_strict("JARVIS_X", 1)

    def test_out_of_range_raises(self, monkeypatch):
        monkeypatch.setenv("JARVIS_X", "99")
        with pytest.raises(ValueError, match="不能大于"):
            _parse_int_strict("JARVIS_X", 1, max_val=2)

    def test_artifact_limits_from_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ARTIFACT_ROOT", "/tmp/jarvis-artifacts")
        monkeypatch.setenv("JARVIS_ARTIFACT_INLINE_MAX_BYTES", "32")
        monkeypatch.setenv("JARVIS_ARTIFACT_MAX_FILE_BYTES", "2048")
        cfg = WorkerConfig.from_env()
        assert cfg.artifact_root == "/tmp/jarvis-artifacts"
        assert cfg.artifact_inline_max_bytes == 32
        assert cfg.artifact_max_file_bytes == 2048

    @pytest.mark.parametrize(
        ("name", "value"),
        (
            ("JARVIS_MODEL_TIMEOUT_SECONDS", "0"),
            ("JARVIS_MODEL_TIMEOUT_SECONDS", "601"),
            ("JARVIS_MODEL_MAX_TOKENS", "0"),
            ("JARVIS_MODEL_MAX_TOKENS", "131073"),
        ),
    )
    def test_model_capacity_limits_reject_out_of_range(
        self, monkeypatch, name, value
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match="不能小于|不能大于"):
            WorkerConfig.from_env()

    def test_model_capacity_limits_accept_exact_boundaries(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_TIMEOUT_SECONDS", "600")
        monkeypatch.setenv("JARVIS_MODEL_MAX_TOKENS", "131072")
        monkeypatch.setenv("JARVIS_MODEL_CONTEXT_WINDOW_TOKENS", "132097")

        cfg = WorkerConfig.from_env()

        assert cfg.model_timeout_seconds == 600
        assert cfg.model_max_tokens == 131_072

    def test_agent_tool_budget_defaults_to_ten_and_is_configurable(self, monkeypatch):
        assert WorkerConfig().agent_max_iterations == 14
        monkeypatch.setenv("JARVIS_AGENT_MAX_ITERATIONS", "20")

        assert WorkerConfig.from_env().agent_max_iterations == 20

    @pytest.mark.parametrize("value", ("0", "21", "invalid"))
    def test_agent_tool_budget_rejects_unbounded_or_invalid_values(
        self, monkeypatch, value
    ):
        monkeypatch.setenv("JARVIS_AGENT_MAX_ITERATIONS", value)

        with pytest.raises(ValueError, match="JARVIS_AGENT_MAX_ITERATIONS"):
            WorkerConfig.from_env()

    def test_artifact_capacity_limits_are_loaded_and_ordered(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ARTIFACT_MAX_FILE_BYTES", "1024")
        monkeypatch.setenv("JARVIS_ARTIFACT_MAX_RUN_BYTES", "2048")
        monkeypatch.setenv("JARVIS_ARTIFACT_MAX_WORKSPACE_BYTES", "4096")
        monkeypatch.setenv("JARVIS_ARTIFACT_MAX_TOTAL_BYTES", "8192")

        cfg = WorkerConfig.from_env()

        assert cfg.artifact_max_file_bytes == 1024
        assert cfg.artifact_max_run_bytes == 2048
        assert cfg.artifact_max_workspace_bytes == 4096
        assert cfg.artifact_max_total_bytes == 8192

    def test_artifact_capacity_rejects_inverted_scope_limits(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ARTIFACT_MAX_FILE_BYTES", "4096")
        monkeypatch.setenv("JARVIS_ARTIFACT_MAX_RUN_BYTES", "2048")
        monkeypatch.setenv("JARVIS_ARTIFACT_MAX_WORKSPACE_BYTES", "8192")
        monkeypatch.setenv("JARVIS_ARTIFACT_MAX_TOTAL_BYTES", "16384")

        with pytest.raises(ValueError, match="file <= run <= workspace <= total"):
            WorkerConfig.from_env()


class TestMemoryExtractionConfig:
    def test_defaults_enabled_with_bounded_retry(self):
        cfg = WorkerConfig()
        assert cfg.memory_extraction_enabled is True
        assert cfg.memory_extraction_max_attempts == 3
        assert cfg.memory_candidate_expiry_poll_interval_ms == 60_000

    def test_invalid_enabled_value_fails_closed(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MEMORY_EXTRACTION_ENABLED", "sometimes")
        with pytest.raises(ValueError, match="true 或 false"):
            _parse_bool_strict("JARVIS_MEMORY_EXTRACTION_ENABLED", True)

    def test_candidate_expiry_interval_is_bounded(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_MEMORY_CANDIDATE_EXPIRY_POLL_INTERVAL_MS", "999"
        )
        with pytest.raises(ValueError, match="不能小于"):
            WorkerConfig.from_env()


class TestBaseUrlValidation:
    def test_http_ok(self):
        cfg = WorkerConfig(model_provider="custom_openai_compatible", model_base_url="http://localhost:8000/v1", model_name="m", model_api_key_env="K")
        assert cfg.model_base_url == "http://localhost:8000/v1"

    def test_userinfo_rejected(self):
        cfg = WorkerConfig(model_provider="custom_openai_compatible", model_base_url="https://user:pass@api.example.com/v1", model_name="m", model_api_key_env="K")
        with pytest.raises(ModelProviderError, match="用户名|密码"):
            create_model_provider(cfg, prompt_builder=PromptBuilder())

    def test_query_rejected(self):
        cfg = WorkerConfig(model_provider="custom_openai_compatible", model_base_url="https://api.example.com/v1?debug=1", model_name="m", model_api_key_env="K")
        with pytest.raises(ModelProviderError, match="query|fragment"):
            create_model_provider(cfg, prompt_builder=PromptBuilder())


class TestThinkingMode:
    def test_default_empty(self):
        assert WorkerConfig().model_thinking_mode == ""

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "deepseek")
        monkeypatch.setenv("JARVIS_MODEL_THINKING_MODE", "disabled")
        assert WorkerConfig.from_env().model_thinking_mode == "disabled"

    def test_invalid_raises(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_THINKING_MODE", "enabled")
        with pytest.raises(ValueError, match="THINKING_MODE"):
            WorkerConfig.from_env()

    def test_custom_provider_cannot_use_deepseek_extension(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "custom_openai_compatible")
        monkeypatch.setenv("JARVIS_MODEL_THINKING_MODE", "disabled")
        with pytest.raises(ValueError, match="仅支持 deepseek"):
            WorkerConfig.from_env()


class TestDefaultEnvPath:
    def test_points_to_agent_worker_dotenv(self):
        assert DEFAULT_LOCAL_ENV_PATH.name == ".env"
        assert DEFAULT_LOCAL_ENV_PATH.parent.name == "agent-worker"


# ── Phase 6: URL sanitization ──


class TestSanitizeBaseUrl:
    def test_preserves_standard_https_url(self):
        assert sanitize_base_url("https://api.example.com/v1") == "https://api.example.com/v1"

    def test_preserves_port_and_path(self):
        assert sanitize_base_url("https://api.example.com:8443/chat/v1") == "https://api.example.com:8443/chat/v1"

    def test_removes_userinfo(self):
        result = sanitize_base_url("https://user:pass@api.example.com/v1")
        assert result == "https://api.example.com/v1"
        assert "user" not in result

    def test_removes_query(self):
        result = sanitize_base_url("https://api.example.com/v1?debug=1&token=secret")
        assert result == "https://api.example.com/v1"
        assert "?" not in result

    def test_removes_fragment(self):
        result = sanitize_base_url("https://api.example.com/v1#section")
        assert result == "https://api.example.com/v1"

    def test_removes_query_and_fragment_both(self):
        assert sanitize_base_url("https://api.example.com/v1?a=1#frag") == "https://api.example.com/v1"

    def test_empty_string_returns_empty(self):
        assert sanitize_base_url("") == ""

    def test_invalid_scheme_returns_error_marker(self):
        assert sanitize_base_url("ftp://files.example.com") == "<invalid-scheme>"

    def test_no_hostname_returns_error(self):
        assert sanitize_base_url("https:///path") == "<invalid-url>"

    def test_ip_address_ok(self):
        assert sanitize_base_url("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/v1"

    def test_localhost_ok(self):
        assert sanitize_base_url("http://localhost:11434/v1") == "http://localhost:11434/v1"

    def test_root_path_strips_trailing_slash(self):
        assert sanitize_base_url("https://api.example.com/") == "https://api.example.com"

    def test_invalid_port_returns_marker(self):
        result = sanitize_base_url("https://api.example.com:99999/v1")
        assert result == "<invalid-port>"

    def test_negative_port_returns_marker(self):
        result = sanitize_base_url("https://api.example.com:-1/v1")
        assert result == "<invalid-port>"

    def test_zero_port_returns_marker(self):
        result = sanitize_base_url("https://api.example.com:0/v1")
        assert result == "<invalid-port>"

    def test_non_numeric_port_returns_marker(self):
        result = sanitize_base_url("https://api.example.com:abc/v1")
        assert result == "<invalid-port>"


class TestValidateBaseUrl:
    """复用生产 _validate_model_base_url 规则。"""
    def test_valid_https(self):
        assert _validate_model_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"

    def test_valid_http_with_port(self):
        assert _validate_model_base_url("http://localhost:8080/v1") == "http://localhost:8080/v1"

    def test_trailing_slash_removed(self):
        assert _validate_model_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"

    def test_rejects_userinfo(self):
        with pytest.raises(ValueError, match="用户名"):
            _validate_model_base_url("https://user:pass@api.example.com")

    def test_rejects_query(self):
        with pytest.raises(ValueError, match="query"):
            _validate_model_base_url("https://api.example.com/v1?key=val")

    def test_rejects_fragment(self):
        with pytest.raises(ValueError, match="fragment"):
            _validate_model_base_url("https://api.example.com/v1#section")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="缺失或为空"):
            _validate_model_base_url("")

    def test_rejects_ftp(self):
        with pytest.raises(ValueError, match="http/https"):
            _validate_model_base_url("ftp://files.example.com")

    def test_rejects_no_hostname(self):
        with pytest.raises(ValueError, match="hostname"):
            _validate_model_base_url("https:///path")

    def test_rejects_invalid_port(self):
        with pytest.raises(ValueError, match="端口|Port"):
            _validate_model_base_url("https://api.example.com:99999/v1")


# ── Phase 6: config projection ──


class TestBuildModelConfig:
    def test_not_configured_when_env_empty(self, monkeypatch):
        monkeypatch.delenv("JARVIS_MODEL_BASE_URL", raising=False)
        monkeypatch.delenv("JARVIS_MODEL_NAME", raising=False)
        monkeypatch.delenv("JARVIS_MODEL_API_KEY_ENV", raising=False)
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "custom_openai_compatible")

        config = build_model_config()
        assert config.provider == "custom_openai_compatible"
        assert config.protocol == "openai_chat_completions"
        assert config.model_name == ""
        assert config.api_key_configured is False

    def test_configured_with_valid_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "custom_openai_compatible")
        monkeypatch.setenv("JARVIS_MODEL_BASE_URL", "https://api.openai.com/v1")
        monkeypatch.setenv("JARVIS_MODEL_NAME", "gpt-4o")
        monkeypatch.setenv("JARVIS_MODEL_API_KEY_ENV", "MY_KEY")
        monkeypatch.setenv("MY_KEY", "sk-test123")
        monkeypatch.setenv("JARVIS_MODEL_TIMEOUT_SECONDS", "60")
        monkeypatch.setenv("JARVIS_MODEL_MAX_RETRIES", "1")
        monkeypatch.setenv("JARVIS_MODEL_MAX_TOKENS", "8192")
        monkeypatch.setenv("JARVIS_MODEL_THINKING_MODE", "")

        config = build_model_config()
        assert config.provider == "custom_openai_compatible"
        assert config.protocol == "openai_chat_completions"
        assert config.model_name == "gpt-4o"
        assert config.base_url_display == "https://api.openai.com/v1"
        assert config.api_key_configured is True
        assert config.timeout_seconds == 60
        assert config.max_retries == 1
        assert config.max_tokens == 8192
        assert config.thinking_mode == ""

    def test_api_key_env_set_but_no_value(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_API_KEY_ENV", "MY_KEY")
        monkeypatch.delenv("MY_KEY", raising=False)
        monkeypatch.setenv("JARVIS_MODEL_BASE_URL", "https://api.example.com/v1")
        monkeypatch.setenv("JARVIS_MODEL_NAME", "m")
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "custom_openai_compatible")

        config = build_model_config()
        assert config.api_key_configured is False

    def test_url_sanitized_in_projection(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "custom_openai_compatible")
        monkeypatch.setenv("JARVIS_MODEL_BASE_URL", "https://user:pass@api.example.com/v1?key=val#frag")
        monkeypatch.setenv("JARVIS_MODEL_NAME", "m")
        monkeypatch.setenv("JARVIS_MODEL_API_KEY_ENV", "K")
        monkeypatch.setenv("K", "sk-test")

        config = build_model_config()
        assert config.base_url_display == "https://api.example.com/v1"

    def test_worker_status_included(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "custom_openai_compatible")
        config = build_model_config(
            worker_status="idle",
            last_heartbeat_at="2026-01-01T00:00:00Z",
            last_error_code="MODEL_TIMEOUT",
        )
        assert config.worker_status == "idle"
        assert config.last_heartbeat_at == "2026-01-01T00:00:00Z"
        assert config.last_error_code == "MODEL_TIMEOUT"

    def test_fallback_on_broken_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_TIMEOUT_SECONDS", "not_a_number")
        config = build_model_config()
        # 应为降级结果，不抛异常
        assert config.provider == ""


# ── Phase 6: connectivity test (with dependency injection) ──


async def _noop_audit(**_kwargs):
    """避免连通性单测依赖 PostgreSQL。"""


@pytest.fixture
def _valid_model_env(monkeypatch):
    """配置完整的合法模型环境。"""
    monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "custom_openai_compatible")
    monkeypatch.setenv("JARVIS_MODEL_BASE_URL", "https://api.test.com/v1")
    monkeypatch.setenv("JARVIS_MODEL_NAME", "test-model")
    monkeypatch.setenv("JARVIS_MODEL_API_KEY_ENV", "MY_KEY")
    monkeypatch.setenv("MY_KEY", "sk-test")
    # 禁用 AuditLog 写入（避免数据库依赖）
    monkeypatch.setattr(
        "jarvis_worker.control_plane.model_config._write_audit_via_service",
        _noop_audit,
    )


class TestModelConnection:
    """连通性测试（全部通过 _client_factory 注入 MockTransport，零网络访问）。"""

    def test_success_200(self, _valid_model_env):
        """200 → status=ok。"""

        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]
            })

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection

            def client_factory():
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), timeout=5.0
                )

            result = await test_model_connection(_client_factory=client_factory)
            assert result.status == "ok"
            assert result.provider == "custom_openai_compatible"
            assert result.model == "test-model"
            assert result.latency_ms > 0
            assert result.error_code is None

        asyncio.new_event_loop().run_until_complete(run())

    def test_auth_401(self, _valid_model_env):
        """401 → MODEL_AUTH_ERROR。"""

        def handler(request):
            return httpx.Response(401, json={"error": "unauthorized"})

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection

            def client_factory():
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), timeout=5.0
                )

            result = await test_model_connection(_client_factory=client_factory)
            assert result.status == "failed"
            assert result.error_code == "MODEL_AUTH_ERROR"

        asyncio.new_event_loop().run_until_complete(run())

    def test_auth_403(self, _valid_model_env):
        """403 → MODEL_AUTH_ERROR。"""

        def handler(request):
            return httpx.Response(403, json={"error": "forbidden"})

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection

            def client_factory():
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), timeout=5.0
                )

            result = await test_model_connection(_client_factory=client_factory)
            assert result.status == "failed"
            assert result.error_code == "MODEL_AUTH_ERROR"

        asyncio.new_event_loop().run_until_complete(run())

    def test_not_found_404(self, _valid_model_env):
        """404 → MODEL_HTTP_ERROR。"""

        def handler(request):
            return httpx.Response(404, json={"error": "not found"})

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection

            def client_factory():
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), timeout=5.0
                )

            result = await test_model_connection(_client_factory=client_factory)
            assert result.status == "failed"
            assert result.error_code == "MODEL_HTTP_ERROR"
            assert "404" in result.error_message

        asyncio.new_event_loop().run_until_complete(run())

    def test_server_error_500(self, _valid_model_env):
        """500 → MODEL_HTTP_ERROR。"""

        def handler(request):
            return httpx.Response(500, json={"error": "internal"})

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection

            def client_factory():
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), timeout=5.0
                )

            result = await test_model_connection(_client_factory=client_factory)
            assert result.status == "failed"
            assert result.error_code == "MODEL_HTTP_ERROR"

        asyncio.new_event_loop().run_until_complete(run())

    def test_timeout(self, _valid_model_env):
        """httpx.TimeoutException → MODEL_TIMEOUT。"""

        def handler(request):
            raise httpx.TimeoutException("timeout")

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection

            def client_factory():
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), timeout=5.0
                )

            result = await test_model_connection(_client_factory=client_factory)
            assert result.status == "failed"
            assert result.error_code == "MODEL_TIMEOUT"

        asyncio.new_event_loop().run_until_complete(run())

    def test_connect_error(self, _valid_model_env):
        """httpx.ConnectError → MODEL_HTTP_ERROR。"""

        def handler(request):
            raise httpx.ConnectError("connection refused")

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection

            def client_factory():
                return httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), timeout=5.0
                )

            result = await test_model_connection(_client_factory=client_factory)
            assert result.status == "failed"
            assert result.error_code == "MODEL_HTTP_ERROR"

        asyncio.new_event_loop().run_until_complete(run())

    def test_missing_base_url(self, monkeypatch):
        """缺少 base URL → MODEL_CONFIG_ERROR（不联网）。"""
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "custom_openai_compatible")
        monkeypatch.delenv("JARVIS_MODEL_BASE_URL", raising=False)
        monkeypatch.setenv("JARVIS_MODEL_NAME", "test-model")
        monkeypatch.setenv("JARVIS_MODEL_API_KEY_ENV", "MY_KEY")
        monkeypatch.setenv("MY_KEY", "sk-test")
        monkeypatch.setattr(
            "jarvis_worker.control_plane.model_config._write_audit_via_service",
            _noop_audit,
        )

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection
            result = await test_model_connection()
            assert result.status == "failed"
            assert result.error_code == "MODEL_CONFIG_ERROR"

        asyncio.new_event_loop().run_until_complete(run())

    def test_missing_api_key(self, monkeypatch):
        """缺少 API key → MODEL_CONFIG_ERROR（不联网）。"""
        monkeypatch.setenv("JARVIS_MODEL_PROVIDER", "custom_openai_compatible")
        monkeypatch.setenv("JARVIS_MODEL_BASE_URL", "https://api.test.com/v1")
        monkeypatch.setenv("JARVIS_MODEL_NAME", "test-model")
        monkeypatch.setenv("JARVIS_MODEL_API_KEY_ENV", "MY_KEY")
        monkeypatch.delenv("MY_KEY", raising=False)
        monkeypatch.setattr(
            "jarvis_worker.control_plane.model_config._write_audit_via_service",
            _noop_audit,
        )

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection
            result = await test_model_connection()
            assert result.status == "failed"
            assert result.error_code == "MODEL_CONFIG_ERROR"

        asyncio.new_event_loop().run_until_complete(run())

    def test_invalid_url_userinfo(self, _valid_model_env, monkeypatch):
        """URL 含 userinfo → MODEL_CONFIG_ERROR（不联网）。"""
        monkeypatch.setenv("JARVIS_MODEL_BASE_URL", "https://user:pass@api.test.com/v1")

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection
            result = await test_model_connection()
            assert result.status == "failed"
            assert result.error_code == "MODEL_CONFIG_ERROR"

        asyncio.new_event_loop().run_until_complete(run())

    def test_invalid_url_query(self, _valid_model_env, monkeypatch):
        """URL 含 query → MODEL_CONFIG_ERROR（不联网）。"""
        monkeypatch.setenv("JARVIS_MODEL_BASE_URL", "https://api.test.com/v1?debug=1")

        async def run():
            from jarvis_worker.control_plane.model_config import test_model_connection
            result = await test_model_connection()
            assert result.status == "failed"
            assert result.error_code == "MODEL_CONFIG_ERROR"

        asyncio.new_event_loop().run_until_complete(run())

    def test_audit_written_on_success(self, _valid_model_env):
        """成功时调用 _write_audit_via_service。"""
        called = []

        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]
            })

        async def run():
            import jarvis_worker.control_plane.model_config as mc
            from jarvis_worker.control_plane.model_config import (
                test_model_connection,
            )

            original = mc._write_audit_via_service
            async def capture_audit(**kw):
                called.append(kw)

            mc._write_audit_via_service = capture_audit

            try:
                def client_factory():
                    return httpx.AsyncClient(
                        transport=httpx.MockTransport(handler), timeout=5.0
                    )

                result = await test_model_connection(_client_factory=client_factory)
                assert result.status == "ok"
                assert len(called) == 1
                assert called[0]["status"] == "ok"
                assert called[0]["provider"] == "custom_openai_compatible"
                assert "error_code" not in called[0] or called[0]["error_code"] is None
            finally:
                mc._write_audit_via_service = original

        asyncio.new_event_loop().run_until_complete(run())

    def test_audit_written_on_failure(self, _valid_model_env):
        """失败时调用 _write_audit_via_service，含错误信息。"""
        called = []

        def handler(request):
            return httpx.Response(401, json={"error": "unauthorized"})

        async def run():
            import jarvis_worker.control_plane.model_config as mc
            from jarvis_worker.control_plane.model_config import (
                test_model_connection,
            )

            original = mc._write_audit_via_service
            async def capture_audit(**kw):
                called.append(kw)

            mc._write_audit_via_service = capture_audit

            try:
                def client_factory():
                    return httpx.AsyncClient(
                        transport=httpx.MockTransport(handler), timeout=5.0
                    )

                result = await test_model_connection(_client_factory=client_factory)
                assert result.status == "failed"
                assert len(called) == 1
                assert called[0]["status"] == "failed"
                assert called[0]["error_code"] == "MODEL_AUTH_ERROR"
            finally:
                mc._write_audit_via_service = original

        asyncio.new_event_loop().run_until_complete(run())
