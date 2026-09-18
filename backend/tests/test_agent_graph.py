"""Agent 图结构测试（使用假模型，不调用外部 API）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app import db
from app.agent import graph as graph_module
from app.config import get_settings


class _StructuredModel:
    def invoke(self, _: Any) -> Any:  # pragma: no cover - 本测试只验证图编译
        raise AssertionError("不应调用模型")


class _FakeLLM:
    def with_structured_output(self, _: Any) -> _StructuredModel:
        return _StructuredModel()


@pytest.fixture()
def configured_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> graph_module.AgentRuntime:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _FakeLLM())
    runtime = graph_module.AgentRuntime()
    yield runtime
    get_settings.cache_clear()
    db._initialized = False


def test_agent_graph_compiles(configured_runtime: graph_module.AgentRuntime) -> None:
    assert configured_runtime.graph is not None
    graph = configured_runtime.graph.get_graph()
    assert {"perceive", "planning", "act", "approval", "observe", "reflecting"}.issubset(
        set(graph.nodes)
    )
