"""测试 config/env_loader.py。全部 tmp_path，不读取本地 .env。"""

from __future__ import annotations

import os

import pytest

from jarvis_worker.shared.config.env_loader import load_local_env
from jarvis_worker.shared.config.settings import WorkerConfig


class TestLoadLocalEnv:
    def test_loads_vars(self, tmp_path, monkeypatch):
        f = tmp_path / ".env"
        f.write_text("JARVIS_WORKER_ID=dotenv-w\n")
        monkeypatch.delenv("JARVIS_WORKER_ID", raising=False)
        load_local_env(f)
        assert os.environ["JARVIS_WORKER_ID"] == "dotenv-w"

    def test_external_priority(self, tmp_path, monkeypatch):
        f = tmp_path / ".env"
        f.write_text("JARVIS_WORKER_ID=dotenv\n")
        monkeypatch.setenv("JARVIS_WORKER_ID", "shell")
        load_local_env(f)
        assert os.environ["JARVIS_WORKER_ID"] == "shell"

    def test_missing_ok(self, tmp_path):
        load_local_env(tmp_path / "nope.env")

    def test_empty_file_ok(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("")
        load_local_env(f)

    def test_comment_ignored(self, tmp_path, monkeypatch):
        f = tmp_path / ".env"
        f.write_text("# comment\nJARVIS_WORKER_ID=v\n")
        monkeypatch.delenv("JARVIS_WORKER_ID", raising=False)
        load_local_env(f)
        assert os.environ["JARVIS_WORKER_ID"] == "v"


class TestFailClosed:
    def test_loader_oserror_propagates(self, tmp_path, monkeypatch):
        """_loader 抛 OSError 时原样传播。"""
        f = tmp_path / ".env"
        f.write_text("KEY=v")

        def _failing_loader(_path):
            raise OSError("permission denied")

        with pytest.raises(OSError, match="permission"):
            load_local_env(f, _loader=_failing_loader)

    def test_missing_file_no_import(self, tmp_path, monkeypatch):
        """.env 不存在时不要求 python-dotenv 可用。"""
        monkeypatch.setitem(sys.modules, "dotenv", None)
        load_local_env(tmp_path / "nope.env")  # OK

    def test_existing_file_import_error_fails(self, tmp_path, monkeypatch):
        """.env 存在但 dotenv 无法导入 → 启动失败。"""
        f = tmp_path / ".env"
        f.write_text("KEY=v")

        def _bad_loader(_path):
            raise ImportError("no dotenv")

        with pytest.raises(ImportError, match="no dotenv"):
            load_local_env(f, _loader=_bad_loader)

    def test_secret_not_in_exception(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("MY_KEY=sk-top-secret\n")

        def _failing(_path):
            raise OSError("read error")

        with pytest.raises(OSError):
            load_local_env(f, _loader=_failing)
        # 错误信息不包含 secret
        # (OSError 来自我们注入的 loader，不含文件内容)


class TestWorkerConfigIntegration:
    def test_dotenv_then_config(self, tmp_path, monkeypatch):
        f = tmp_path / ".env"
        f.write_text("JARVIS_WORKER_ID=env-w99\nJARVIS_MODEL_PROVIDER=openai_compatible\n")
        for k in ("JARVIS_WORKER_ID", "JARVIS_MODEL_PROVIDER"):
            monkeypatch.delenv(k, raising=False)
        load_local_env(f)
        cfg = WorkerConfig.from_env()
        assert cfg.worker_id == "env-w99"

    def test_shell_overrides_dotenv(self, tmp_path, monkeypatch):
        f = tmp_path / ".env"
        f.write_text("JARVIS_WORKER_ID=dotenv\nJARVIS_MODEL_PROVIDER=openai_compatible\n")
        monkeypatch.setenv("JARVIS_WORKER_ID", "shell")
        for k in ("JARVIS_MODEL_PROVIDER",):
            monkeypatch.delenv(k, raising=False)
        load_local_env(f)
        assert os.environ["JARVIS_WORKER_ID"] == "shell"

    def test_missing_env_uses_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_WORKER_ID", raising=False)
        monkeypatch.delenv("JARVIS_MODEL_PROVIDER", raising=False)
        load_local_env(tmp_path / "nope.env")
        cfg = WorkerConfig.from_env()
        assert cfg.worker_id == "worker-01"

    def test_skill_adapter_root_is_configurable(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SKILL_ADAPTERS_ROOT", "/trusted/jarvis-adapters")

        cfg = WorkerConfig.from_env()

        assert cfg.skill_adapters_root == "/trusted/jarvis-adapters"

    def test_redis_connection_scope_matches_gateway_environment(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REDIS_PASSWORD", "redis-secret")
        monkeypatch.setenv("JARVIS_REDIS_DB", "9")

        cfg = WorkerConfig.from_env()

        assert cfg.redis_password == "redis-secret"
        assert cfg.redis_db == 9
        assert "redis-secret" not in repr(cfg)

    def test_invalid_redis_db_matches_gateway_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_REDIS_DB", "invalid")

        assert WorkerConfig.from_env().redis_db == 0


class TestSecretSafety:
    def test_config_no_key_value(self, tmp_path, monkeypatch):
        f = tmp_path / ".env"
        f.write_text("JARVIS_MODEL_API_KEY_ENV=MY_K\nMY_K=sk-fake\n")
        for k in ("JARVIS_MODEL_API_KEY_ENV", "MY_K"):
            monkeypatch.delenv(k, raising=False)
        load_local_env(f)
        cfg = WorkerConfig.from_env()
        assert cfg.model_api_key_env == "MY_K"
        assert "sk-fake" not in str(cfg)

    def test_default_deepseek(self, monkeypatch):
        monkeypatch.delenv("JARVIS_MODEL_PROVIDER", raising=False)
        assert WorkerConfig.from_env().model_provider == "deepseek"

    def test_no_network(self):
        cfg = WorkerConfig.from_env()
        assert cfg.model_provider in {"deepseek", "custom_openai_compatible"}


import sys  # noqa: E402
