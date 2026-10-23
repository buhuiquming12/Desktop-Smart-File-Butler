"""完成判定强化：模型声称完成不能覆盖后端事实。

- 用户要求 move/rename/delete，但本次运行只有扫描/摘要/只读查询 → ``goal_unmet``；
- 明确要求新建目录但没建 → 仍然是 partial（目标未完全达成）；
- 工具结果里的后置条件（文件真实存在）才是 success 的依据。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.outcome import evaluate_completion, record_verification
from app.agent.tool_executor import ToolExecutor
from app import db
from app.config import get_settings
from scopes import scope_for


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("SANDBOX_ROOTS", str(root))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield root
    get_settings.cache_clear()
    db._initialized = False


def _scan_observation(root: Path) -> dict:
    return record_verification(
        ToolExecutor().execute(
            {"id": "s", "tool": "scan_directory", "args": {"directory": str(root)}}
        )
    )


def test_scan_only_cannot_complete_a_move_goal(sandbox: Path) -> None:
    (sandbox / "a.pdf").write_text("x", encoding="utf-8")
    observation = _scan_observation(sandbox)
    assert observation["status"] == "ok"

    completion = evaluate_completion({
        "observations": [observation],
        "plan": [{"id": "m", "tool": "move_file", "description": "移动"}],
        "step_index": 1,
        "trusted_user_intent": "把这些 PDF 移动到归档目录",
        "mutations": {"move": 0, "rename": 0, "delete": 0},
    })
    assert completion["outcome"] == "goal_unmet"
    assert completion["stop_reason"] == "goal_unmet"
    assert "move" in completion["unmet_effects"]
    assert "目标未达成" in completion["message"]


def test_summary_only_cannot_complete_a_delete_goal(sandbox: Path) -> None:
    target = sandbox / "old.txt"
    target.write_text("x", encoding="utf-8")
    summary = record_verification(
        ToolExecutor().execute(
            {
                "id": "w",
                "tool": "write_summary",
                "args": {"file_path": str(target), "output_dir": str(sandbox / "out")},
            },
            authorization_scope=scope_for(sandbox, target_roots=[sandbox / "out"]),
        )
    )
    # 摘要工具需要模型；没有模型时明确失败，这里直接用一条只读观察代替。
    if summary["status"] != "ok":
        summary = _scan_observation(sandbox)

    completion = evaluate_completion({
        "observations": [summary],
        "plan": [{"id": "d", "tool": "delete_file", "description": "删除"}],
        "step_index": 1,
        "trusted_user_intent": "删除这个旧文件",
        "mutations": {"move": 0, "rename": 0, "delete": 0},
    })
    assert completion["outcome"] == "goal_unmet"
    assert "delete" in completion["unmet_effects"]
    assert target.exists()


def test_real_move_satisfies_goal(sandbox: Path) -> None:
    source = sandbox / "a.txt"
    source.write_text("x", encoding="utf-8")
    moved = record_verification(
        ToolExecutor().execute(
            {
                "id": "m",
                "tool": "move_file",
                "args": {"src": str(source), "dest_dir": str(sandbox / "out")},
            },
            authorization_scope=scope_for(sandbox),
        )
    )
    assert moved["status"] == "ok"
    completion = evaluate_completion({
        "observations": [moved],
        "plan": [{"id": "m", "tool": "move_file", "description": "移动"}],
        "step_index": 1,
        "trusted_user_intent": "把 a.txt 移动到 out 目录",
        "mutations": {"move": 1, "rename": 0, "delete": 0},
    })
    assert completion["outcome"] == "success"
    assert completion["unmet_effects"] == []


def test_missing_create_target_is_partial_not_success() -> None:
    completion = evaluate_completion({
        "observations": [],
        "plan": [],
        "step_index": 0,
        "trusted_user_intent": "新建一个备份文件夹",
        "mutations": {},
    })
    assert completion["outcome"] == "partial"
    assert completion["unmet_effects"] == ["create"]
