from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import db
from app.config import get_settings
from app.tools import filesystem


def test_concurrent_moves_choose_distinct_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    left, right, destination = tmp_path / "left", tmp_path / "right", tmp_path / "dest"
    left.mkdir(); right.mkdir(); destination.mkdir()
    (left / "same.txt").write_text("left", encoding="utf-8")
    (right / "same.txt").write_text("right", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda p: filesystem.move_file(str(p), str(destination)), [left / "same.txt", right / "same.txt"]))
    assert len(set(results)) == 2
    assert {p.read_text(encoding="utf-8") for p in destination.iterdir()} == {"left", "right"}
    get_settings.cache_clear()
    db._initialized = False
