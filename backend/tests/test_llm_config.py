"""P2 补测试：llm_config 的覆盖优先级（DB vs .env）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db, llm_config
from app.config import get_settings


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "env-model")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield tmp_path
    get_settings.cache_clear()
    db._initialized = False


def test_env_used_without_override(env) -> None:
    config = llm_config.get_effective_config()
    assert config.openai_model == "env-model"
    assert config.openai_api_key == "env-key"


def test_db_override_beats_env(env) -> None:
    llm_config.save_overrides({"openai_model": "db-model"})
    config = llm_config.get_effective_config()
    assert config.openai_model == "db-model"      # 覆盖生效
    assert config.openai_api_key == "env-key"      # 未覆盖的仍用 .env


def test_empty_override_falls_back_to_env(env) -> None:
    llm_config.save_overrides({"openai_model": "db-model"})
    llm_config.save_overrides({"openai_model": ""})  # 空串清除覆盖
    assert llm_config.get_effective_config().openai_model == "env-model"


def test_provider_lowercased(env) -> None:
    llm_config.save_overrides({"provider": "OpenAI"})
    assert llm_config.get_effective_config().provider == "openai"


def test_unknown_keys_ignored(env) -> None:
    llm_config.save_overrides({"not_a_real_key": "x", "openai_model": "db-model"})
    assert llm_config.get_effective_config().openai_model == "db-model"
    assert "not_a_real_key" not in db.get_llm_config()
