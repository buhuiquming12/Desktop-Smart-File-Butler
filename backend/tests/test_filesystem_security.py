"""文件工具和沙箱的最小单元测试（不需要模型 API）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.config import get_settings
from app.security import SandboxViolation, resolve_in_sandbox
from app.tools import filesystem


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield tmp_path
    get_settings.cache_clear()
    db._initialized = False


def test_rejects_path_outside_sandbox(sandbox: Path) -> None:
    outside = sandbox.parent / "outside.txt"
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(str(outside))


def test_move_renames_collision_without_overwrite(sandbox: Path) -> None:
    source = sandbox / "source.txt"
    source.write_text("new", encoding="utf-8")
    destination = sandbox / "docs"
    destination.mkdir()
    (destination / "source.txt").write_text("old", encoding="utf-8")

    moved = Path(filesystem.move_file(str(source), str(destination)))

    assert moved.name == "source (1).txt"
    assert moved.read_text(encoding="utf-8") == "new"
    assert (destination / "source.txt").read_text(encoding="utf-8") == "old"


def test_rejects_new_name_path_escape(sandbox: Path) -> None:
    source = sandbox / "source.txt"
    source.write_text("content", encoding="utf-8")

    with pytest.raises(ValueError):
        filesystem.rename_file(str(source), "../escaped.txt")

    assert source.exists()
