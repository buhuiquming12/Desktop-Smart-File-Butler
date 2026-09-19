"""P3 regression: rollback preflight marks missing target as failed."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app import db
from app.config import get_settings
from app.models import OperationLog
from app.tools.filesystem import preflight_restore


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolated test DB: preflight reads sandbox roots via DB override."""
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield
    get_settings.cache_clear()
    db._initialized = False


def test_preflight_reports_missing_source(env: None, tmp_path: Path) -> None:
    op = OperationLog(
        action="move",
        target=str(tmp_path / "origin.txt"),
        dest=str(tmp_path / "missing.txt"),
        status="ok",
        ts=datetime.now(),
    )
    result = preflight_restore(op)
    assert result["status"] == "failed"
    assert "\u9884\u68c0" in result["detail"]
