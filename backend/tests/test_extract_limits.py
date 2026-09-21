"""文档提取资源上限：截断必须发生在解析过程中，大文件必须在解析前拒绝。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.config import get_settings
from app.tools import extract


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


def test_oversized_source_rejected_before_extraction(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sandbox / "large.txt"
    target.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(extract, "_MAX_SOURCE_BYTES", 4)

    with pytest.raises(ValueError, match="文件过大"):
        extract.extract_text(str(target))


def test_text_reader_only_reads_needed_prefix(sandbox: Path) -> None:
    target = sandbox / "long.txt"
    target.write_text("abcdefghij", encoding="utf-8")

    result = extract.extract_text(str(target), max_chars=4)

    assert result.startswith("abcd")
    assert "已截断" in result
