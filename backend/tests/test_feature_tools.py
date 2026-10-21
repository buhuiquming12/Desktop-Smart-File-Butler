"""新工具的契约与只读语义：preview_batch / search_content。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import db
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.policy import PolicyEngine
from app.tools import indexer
from app.tools.contracts import TOOL_CONTRACTS, validate_tool_args


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield tmp_path
    get_settings.cache_clear()
    db._initialized = False


def test_new_tools_are_registered_as_read_only() -> None:
    assert TOOL_CONTRACTS["preview_batch"].side_effect == "read"
    assert TOOL_CONTRACTS["search_content"].side_effect == "read"
    assert not TOOL_CONTRACTS["preview_batch"].is_mutation
    assert not TOOL_CONTRACTS["search_content"].is_mutation


def test_preview_batch_rejects_missing_target() -> None:
    _, error = validate_tool_args("preview_batch", {"op": "move"})
    assert error and "scan_id" in error
    _, error = validate_tool_args(
        "preview_batch", {"op": "move", "directory": "x"}
    )
    assert error and "dest_dir" in error
    _, error = validate_tool_args(
        "preview_batch", {"op": "classify", "directory": "x"}
    )
    assert error and "dest_root" in error


def test_preview_batch_is_authorized_without_mutation_intent(sandbox: Path) -> None:
    policy = PolicyEngine()
    decision = policy.evaluate(
        "preview_batch",
        state={"trusted_user_intent": "先预览一下再决定要不要整理"},
        proposed_count=10,
    )
    assert decision.allowed
    assert decision.requires_approval is False


def test_preview_batch_tool_creates_preview_without_moving(sandbox: Path) -> None:
    source = sandbox / "inbox"
    source.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (source / name).write_text("pdf", encoding="utf-8")
    executor = ToolExecutor(None)
    observation = executor.execute({
        "id": "p", "tool": "preview_batch",
        "args": {
            "op": "move", "directory": str(source),
            "dest_dir": str(sandbox / "out"), "filter": {"extensions": ["pdf"]},
        },
    })
    assert observation["status"] == "ok", observation
    result = observation["result"]
    assert result["counts"]["total"] == 2
    assert result["preview_id"]
    assert "只读" in result["note"]
    # 只读：文件仍在原处，目标目录未被创建。
    assert (source / "a.pdf").exists()
    assert not (sandbox / "out").exists()


def test_search_content_tool_returns_hits(sandbox: Path) -> None:
    docs = sandbox / "docs"
    docs.mkdir()
    (docs / "memo.txt").write_text("明天讨论预算与排期", encoding="utf-8")
    job = indexer.start_index(str(docs))
    deadline = time.time() + 15
    while time.time() < deadline:
        if indexer.job_status(job["job_id"])["status"] != "running":
            break
        time.sleep(0.05)
    executor = ToolExecutor(None)
    observation = executor.execute({
        "id": "s", "tool": "search_content", "args": {"query": "预算"},
    })
    assert observation["status"] == "ok", observation
    assert observation["result"]["total"] == 1
    assert observation["result"]["results"][0]["name"] == "memo.txt"


def test_preview_batch_respects_sandbox(sandbox: Path) -> None:
    outside = sandbox.parent / "outside-preview"
    outside.mkdir(exist_ok=True)
    executor = ToolExecutor(None)
    observation = executor.execute({
        "id": "p", "tool": "preview_batch",
        "args": {"op": "move", "directory": str(outside), "dest_dir": str(sandbox / "out")},
    })
    assert observation["status"] == "failed"
    assert "越界" in observation["error"]
