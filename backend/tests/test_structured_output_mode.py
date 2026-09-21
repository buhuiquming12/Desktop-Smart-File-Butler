"""设置页「手动降级结构化输出」开关：配置读取、接口校验与运行时接线。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db, llm_config, main
from app.agent.llm import build_structured_llm
from app.config import get_settings

TOKEN = main.get_session_token()
HEADERS = {"origin": "http://127.0.0.1:5173", "X-Butler-Token": TOKEN}


class _Plan:
    """结构化输出所要求的返回类型（这里只用于构造，不真正校验）。"""

    @classmethod
    def model_json_schema(cls) -> dict:
        return {"type": "object", "properties": {"goal": {"type": "string"}}}

    model_validate = staticmethod(lambda value: value)


class _FakeLLM:
    def __init__(self) -> None:
        self.native_builds = 0

    def with_structured_output(self, schema: Any) -> Any:
        self.native_builds += 1
        return object()

    def invoke(self, messages: Any) -> Any:  # pragma: no cover - 本文件不触发提示词路径
        raise AssertionError("不应调用模型")


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("STRUCTURED_OUTPUT_MODE", raising=False)
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield TestClient(main.app)
    get_settings.cache_clear()
    db._initialized = False


# ---------------- 配置层 ----------------


def test_default_mode_is_auto(client: TestClient) -> None:
    assert llm_config.get_effective_config().structured_output_mode == "auto"
    assert client.get("/api/settings/llm", headers=HEADERS).json()["structured_output_mode"] == "auto"


def test_env_default_can_be_prompt(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTURED_OUTPUT_MODE", "prompt")
    get_settings.cache_clear()
    assert llm_config.get_effective_config().structured_output_mode == "prompt"


def test_db_override_beats_env(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTURED_OUTPUT_MODE", "prompt")
    get_settings.cache_clear()
    llm_config.save_overrides({"structured_output_mode": "auto"})
    assert llm_config.get_effective_config().structured_output_mode == "auto"


def test_corrupted_db_value_falls_back_to_auto(client: TestClient) -> None:
    """DB 里被手改成脏值时收敛为 auto，而不是让结构化输出直接不可用。"""
    db.set_llm_config({"structured_output_mode": "PROMPT_JSON"})
    assert llm_config.get_effective_config().structured_output_mode == "auto"


# ---------------- 接口层 ----------------


def test_put_saves_prompt_mode(client: TestClient) -> None:
    resp = client.put("/api/settings/llm", json={"structured_output_mode": "prompt"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["structured_output_mode"] == "prompt"
    assert db.get_llm_config()["structured_output_mode"] == "prompt"


def test_put_lowercases_mode(client: TestClient) -> None:
    resp = client.put("/api/settings/llm", json={"structured_output_mode": "Prompt"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["structured_output_mode"] == "prompt"


def test_put_rejects_unknown_mode_and_keeps_previous(client: TestClient) -> None:
    client.put("/api/settings/llm", json={"structured_output_mode": "prompt"}, headers=HEADERS)
    resp = client.put("/api/settings/llm", json={"structured_output_mode": "native"}, headers=HEADERS)
    assert resp.status_code == 422
    # 被拒的值不得落库，也不得覆盖已有设置
    assert db.get_llm_config()["structured_output_mode"] == "prompt"


def test_put_empty_string_clears_override(client: TestClient) -> None:
    client.put("/api/settings/llm", json={"structured_output_mode": "prompt"}, headers=HEADERS)
    resp = client.put("/api/settings/llm", json={"structured_output_mode": ""}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["structured_output_mode"] == "auto"
    assert "structured_output_mode" not in db.get_llm_config()


def test_put_resets_runtime_so_next_chat_rebuilds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关必须在下一次会话生效；不重置单例的话旧 runtime 会一直沿用旧模式。"""
    sentinel = object()
    monkeypatch.setattr(main, "_runtime", sentinel)
    client.put("/api/settings/llm", json={"structured_output_mode": "prompt"}, headers=HEADERS)
    assert main._runtime is None


def test_saving_model_config_keeps_mode(client: TestClient) -> None:
    """保存模型配置时不应把开关打回默认值。"""
    client.put("/api/settings/llm", json={"structured_output_mode": "prompt"}, headers=HEADERS)
    resp = client.put("/api/settings/llm", json={"openai_model": "gpt-4o"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["structured_output_mode"] == "prompt"


# ---------------- 与运行时接线 ----------------


def test_build_structured_llm_honours_prompt_mode(client: TestClient) -> None:
    llm_config.save_overrides({"structured_output_mode": "prompt"})
    fake = _FakeLLM()
    structured = build_structured_llm(fake, _Plan)

    assert structured.using_native is False
    assert fake.native_builds == 0  # 手动降级不构造原生 runnable


def test_build_structured_llm_honours_auto_mode(client: TestClient) -> None:
    fake = _FakeLLM()
    structured = build_structured_llm(fake, _Plan)

    assert structured.using_native is True
    assert fake.native_builds == 1
