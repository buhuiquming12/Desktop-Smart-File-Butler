from __future__ import annotations

import sys
from types import SimpleNamespace

from app.tools import extract


def test_ocr_probe_reports_binary_not_found(monkeypatch) -> None:
    class Missing(Exception):
        pass
    fake = SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd=""),
        TesseractNotFoundError=Missing,
        get_tesseract_version=lambda: (_ for _ in ()).throw(Missing()),
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    assert extract.ocr_capability()["status"] == "binary_not_found"


def test_ocr_probe_reports_missing_language_pack(monkeypatch) -> None:
    fake = SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd=""),
        TesseractNotFoundError=FileNotFoundError,
        get_tesseract_version=lambda: "5.4",
        get_languages=lambda config="": ["eng"],
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    result = extract.ocr_capability()
    assert result["status"] == "language_pack_missing"
    assert result["missing_languages"] == ["chi_sim"]
