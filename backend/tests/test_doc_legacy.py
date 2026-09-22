from pathlib import Path

import pytest

from app import db
from app.config import get_settings
from app.tools import extract


def test_doc_is_not_sent_to_python_docx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear(); db._initialized = False; db.init_db()
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"not a docx zip")
    monkeypatch.setattr(extract, "extract_docx", lambda *_args, **_kwargs: pytest.fail(".doc must not use python-docx"))
    assert "不支持旧版 .doc" in extract.extract_text(str(source))
    get_settings.cache_clear(); db._initialized = False
