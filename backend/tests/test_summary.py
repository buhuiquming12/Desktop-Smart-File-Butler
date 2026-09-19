"""P2 回归：长文档 map-reduce 分段摘要，不再静默只摘开头。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.agent import graph as graph_module
from app.config import get_settings
from app.tools import extract


class _CountingLLM:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    def with_structured_output(self, _schema: Any) -> Any:
        return object()

    def invoke(self, messages: List[Any]) -> Any:
        self.calls.append(messages)

        class _R:
            content = "小结"

        return _R()


@pytest.fixture()
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> graph_module.AgentRuntime:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _CountingLLM())
    rt = graph_module.AgentRuntime()
    yield rt
    get_settings.cache_clear()
    db._initialized = False


def test_chunk_text_splits() -> None:
    chunks = extract.chunk_text("abc" * 10, 10)  # 30 字符 → 3 段各 10
    assert chunks == ["abcabcabca", "bcabcabcab", "cabcabcabc"]
    assert extract.chunk_text("", 10) == []
    assert extract.chunk_text("abcde", 10) == ["abcde"]


def test_short_text_single_call(runtime: graph_module.AgentRuntime) -> None:
    result = runtime._summarize_text("doc", "短文本")
    assert result == "小结"
    assert len(runtime.llm.calls) == 1  # 单段：一次调用


def test_long_text_map_reduce(runtime: graph_module.AgentRuntime) -> None:
    long_text = "x" * (graph_module._SUMMARY_CHUNK_CHARS * 5)  # 5 段
    result = runtime._summarize_text("doc", long_text)
    assert result == "小结"
    # 5 段 map + 1 次 reduce = 6 次调用（证明全文都被处理，而非只摘开头）
    assert len(runtime.llm.calls) == 6


def test_over_max_chunks_notes_truncation(runtime: graph_module.AgentRuntime) -> None:
    huge = "y" * (graph_module._SUMMARY_CHUNK_CHARS * (graph_module._SUMMARY_MAX_CHUNKS + 5))
    result = runtime._summarize_text("doc", huge)
    assert "文档过长" in result  # 显式提示截断，而非静默
    # 最多 40 段 map + 1 reduce
    assert len(runtime.llm.calls) == graph_module._SUMMARY_MAX_CHUNKS + 1
