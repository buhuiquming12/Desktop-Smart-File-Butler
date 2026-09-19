from pathlib import Path

from app import db
from app.models import OperationLog
from app.tools.filesystem import preflight_restore


def test_preflight_reports_missing_source(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    op = OperationLog(action="move", target=str(tmp_path / "origin.txt"), dest=str(tmp_path / "missing.txt"), status="ok", ts=__import__("datetime").datetime.now())
    result = preflight_restore(op)
    assert result["status"] == "failed"
    assert "预检" in result["detail"]
