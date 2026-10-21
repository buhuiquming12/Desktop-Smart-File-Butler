"""P4 回归：查找替换、序号、日期模板、非法名/冲突校验，预览与执行一致。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from app import db
from app.agent.tool_executor import ToolExecutor
from app.config import get_settings
from app.tools import filesystem, naming, preview


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


def _scan(executor: ToolExecutor, directory: Path) -> str:
    observation = executor.execute({
        "id": "s", "tool": "scan_directory", "args": {"directory": str(directory)},
    })
    return observation["result"]["scan_id"]


def test_find_replace_and_affixes(sandbox: Path) -> None:
    source = sandbox / "in"
    source.mkdir()
    (source / "Report-2023.pdf").write_text("x", encoding="utf-8")
    page = preview.create({
        "op": "rename", "directory": str(source),
        "find": "Report", "replace": "Summary", "prefix": "final-", "suffix": "-v2",
    })
    item = page["items"][0]
    assert item["target_name"] == "final-Summary-2023-v2.pdf"
    token = preview.confirm(page["preview_id"], [item["item_id"]])["approval_id"]
    preview.execute(page["preview_id"], [item["item_id"]], confirm_token=token)
    assert (source / "final-Summary-2023-v2.pdf").exists()
    assert item["target_name"] == "final-Summary-2023-v2.pdf"


def test_case_sensitive_replace() -> None:
    rule = naming.RenameRule(find="abc", replace="X", case_sensitive=True)
    assert naming.build_name(stem="abcABC", ext=".txt", rule=rule) == "XABC.txt"
    insensitive = naming.RenameRule(find="abc", replace="X")
    assert naming.build_name(stem="abcABC", ext=".txt", rule=insensitive) == "XX.txt"


def test_sequence_prefix_and_padding(sandbox: Path) -> None:
    source = sandbox / "seq"
    source.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (source / name).write_text("x", encoding="utf-8")
    page = preview.create({
        "op": "rename", "directory": str(source),
        "use_sequence": True, "sequence_start": 5, "sequence_step": 2,
        "sequence_padding": 4, "sequence_position": "prefix",
    })
    names = [item["target_name"] for item in page["items"]]
    assert names == ["0005a.txt", "0007b.txt", "0009c.txt"]


def test_scan_order_is_natural_and_stable(sandbox: Path) -> None:
    """扫描顺序必须由自然序决定，而不是文件系统返回目录项的顺序。

    NTFS 通常按字母返回、ext4 按哈希返回；若直接沿用，序号重命名与预览列表在
    Windows 与 Linux 上会得到不同结果（CI 上曾经因此失败）。
    """
    source = sandbox / "order"
    (source / "sub").mkdir(parents=True)
    for name in ("b.txt", "a.txt", "file10.txt", "file2.txt"):
        (source / name).write_text("x", encoding="utf-8")
    (source / "sub" / "z3.txt").write_text("x", encoding="utf-8")

    flat = [item.name for item in filesystem.scan_directory(str(source)).items]
    assert flat == ["a.txt", "b.txt", "file2.txt", "file10.txt", "sub"]

    nested = [
        Path(item.path).relative_to(source).as_posix()
        for item in filesystem.scan_directory(str(source), recursive=True).items
    ]
    assert nested == ["a.txt", "b.txt", "file2.txt", "file10.txt", "sub", "sub/z3.txt"]


def test_date_template_uses_modified_time(sandbox: Path) -> None:
    source = sandbox / "dates"
    source.mkdir()
    path = source / "photo.jpg"
    path.write_text("x", encoding="utf-8")
    stamp = datetime(2023, 11, 14, 12, 30).timestamp()
    os.utime(path, (stamp, stamp))
    page = preview.create({
        "op": "rename", "directory": str(source),
        "use_date": True, "date_source": "modified", "date_format": "YYYY-MM-DD",
        "prefix": "img-",
    })
    item = page["items"][0]
    assert item["target_name"] == "img-photo2023-11-14.jpg"


def test_template_with_sequence_and_date(sandbox: Path) -> None:
    source = sandbox / "tpl"
    source.mkdir()
    path = source / "note.txt"
    path.write_text("x", encoding="utf-8")
    stamp = datetime(2024, 3, 5, 8, 0).timestamp()
    os.utime(path, (stamp, stamp))
    page = preview.create({
        "op": "rename", "directory": str(source),
        "template": "{name}_{date}_{seq}", "date_source": "modified",
        "sequence_padding": 2,
    })
    assert page["items"][0]["target_name"] == "note_20240305_01.txt"


def test_content_date_unavailable_is_skipped(sandbox: Path) -> None:
    source = sandbox / "contentdate"
    source.mkdir()
    (source / "no-date.txt").write_text("这段正文没有任何日期", encoding="utf-8")
    page = preview.create({
        "op": "rename", "directory": str(source),
        "use_date": True, "date_source": "content", "prefix": "x-",
    })
    item = page["items"][0]
    assert item["status"] == "skipped"
    assert "日期" in item["reason"]
    # 不猜测日期：源文件保持原名
    assert (source / "no-date.txt").exists()


def test_content_date_used_when_extraction_succeeds(sandbox: Path) -> None:
    source = sandbox / "contentdate2"
    source.mkdir()
    (source / "invoice.txt").write_text("开票日期 2024-07-09，金额 100 元。", encoding="utf-8")
    page = preview.create({
        "op": "rename", "directory": str(source),
        "use_date": True, "date_source": "content",
        "template": "{name}-{date}", "date_format": "YYYYMMDD",
    })
    assert page["items"][0]["target_name"] == "invoice-20240709.txt"


def test_illegal_name_is_rejected(sandbox: Path) -> None:
    source = sandbox / "illegal"
    source.mkdir()
    (source / "a.txt").write_text("x", encoding="utf-8")
    page = preview.create({
        "op": "rename", "directory": str(source), "prefix": "bad:name-",
    })
    item = page["items"][0]
    assert item["status"] == "skipped"
    assert "非法字符" in item["reason"]


def test_duplicate_targets_are_both_flagged(sandbox: Path) -> None:
    source = sandbox / "dupnames"
    source.mkdir()
    for name in ("a.txt", "b.txt"):
        (source / name).write_text("x", encoding="utf-8")
    page = preview.create({
        "op": "rename", "directory": str(source), "template": "same",
    })
    statuses = {item["status"] for item in page["items"]}
    assert statuses == {"conflict"}
    assert all("重复" in item["reason"] for item in page["items"])
    assert (source / "a.txt").exists() and (source / "b.txt").exists()


def test_batch_rename_tool_matches_preview(sandbox: Path) -> None:
    source = sandbox / "toolrename"
    source.mkdir()
    for name in ("a.txt", "b.txt"):
        (source / name).write_text("x", encoding="utf-8")
    executor = ToolExecutor(None)
    scan_id = _scan(executor, source)
    page = preview.create({
        "op": "rename", "scan_id": scan_id,
        "find": "a", "replace": "A", "use_sequence": True, "sequence_padding": 2,
    })
    expected = sorted(item["target_name"] for item in page["items"])
    result = executor.execute({
        "id": "r", "tool": "batch_rename",
        "args": {
            "scan_id": scan_id, "find": "a", "replace": "A",
            "sequence_start": 1, "sequence_padding": 2,
        },
    })["result"]
    assert result["success"] == 2, result
    actual = sorted(path.name for path in source.iterdir())
    assert actual == expected


def test_batch_rename_requires_some_rule(sandbox: Path) -> None:
    source = sandbox / "emptyrule"
    source.mkdir()
    (source / "a.txt").write_text("x", encoding="utf-8")
    executor = ToolExecutor(None)
    scan_id = _scan(executor, source)
    observation = executor.execute({
        "id": "r", "tool": "batch_rename", "args": {"scan_id": scan_id},
    })
    assert observation["status"] == "failed"
    assert "至少需要" in observation["error"]
