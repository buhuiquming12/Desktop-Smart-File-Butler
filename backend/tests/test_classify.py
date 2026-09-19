"""P2 回归：分类改用 LLM 直接给类别，失败回退规则分类。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app import db
from app.agent import graph as graph_module
from app.config import get_settings


class _LLM:
    def __init__(self, answer: str = "发票", fail: bool = False) -> None:
        self.answer, self.fail = answer, fail

    def with_structured_output(self, _schema: Any) -> Any:
        return object()

    def invoke(self, _messages: Any) -> Any:
        if self.fail:
            raise RuntimeError("模型不可用")

        class _R:
            content = self.answer

        return _R()


@pytest.fixture()
def make_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()

    def _factory(llm: _LLM) -> graph_module.AgentRuntime:
        monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: llm)
        return graph_module.AgentRuntime()

    yield tmp_path, _factory
    get_settings.cache_clear()
    db._initialized = False


def test_llm_category_used(make_runtime) -> None:
    sandbox, factory = make_runtime
    f = sandbox / "doc.txt"
    f.write_text("这是一张增值税发票，金额 100 元。", encoding="utf-8")
    runtime = factory(_LLM(answer="发票"))
    result = runtime._classify_file(str(f))
    assert result["category"] == "发票"
    assert result["rule_category"] == "文档"
    assert result["llm_category"] == "发票"


def test_falls_back_to_rule_on_llm_failure(make_runtime) -> None:
    sandbox, factory = make_runtime
    f = sandbox / "doc.txt"
    f.write_text("一些内容", encoding="utf-8")
    runtime = factory(_LLM(fail=True))
    result = runtime._classify_file(str(f))
    assert result["llm_category"] is None
    assert result["category"] == "文档"  # 回退规则分类


def test_llm_answer_sanitized(make_runtime) -> None:
    sandbox, factory = make_runtime
    f = sandbox / "doc.txt"
    f.write_text("内容", encoding="utf-8")
    runtime = factory(_LLM(answer="类别：财务报表。\n（多余解释）"))
    result = runtime._classify_file(str(f))
    # 去标点/取首行/限长后不含标点与换行
    assert "\n" not in result["category"]
    assert "：" not in result["category"] and "。" not in result["category"]
