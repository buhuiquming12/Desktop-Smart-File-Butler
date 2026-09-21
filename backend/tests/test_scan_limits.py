"""B9 回归：目录扫描的上限。

历史缺陷：``scan_directory`` 用 ``Path.rglob`` 无界遍历。指向盘符根或含
node_modules 的工程目录时会把全部条目读进内存并逐个 stat；``rglob`` 还会跟进
符号链接目录，目录环会让遍历事实上停不下来。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app import db
from app.config import get_settings
from app.tools import filesystem


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


def _names(items) -> set[str]:
    return {item.name for item in items}


def test_recursive_scan_finds_nested_files(sandbox: Path) -> None:
    (sandbox / "a").mkdir()
    (sandbox / "a" / "b").mkdir()
    (sandbox / "top.txt").write_text("x", encoding="utf-8")
    (sandbox / "a" / "mid.txt").write_text("x", encoding="utf-8")
    (sandbox / "a" / "b" / "deep.txt").write_text("x", encoding="utf-8")

    found = _names(filesystem.scan_directory(str(sandbox), recursive=True))
    assert {"top.txt", "mid.txt", "deep.txt", "a", "b"} <= found


def test_non_recursive_scan_stops_at_first_level(sandbox: Path) -> None:
    (sandbox / "sub").mkdir()
    (sandbox / "sub" / "deep.txt").write_text("x", encoding="utf-8")
    (sandbox / "top.txt").write_text("x", encoding="utf-8")

    found = _names(filesystem.scan_directory(str(sandbox), recursive=False))
    assert "top.txt" in found and "sub" in found
    assert "deep.txt" not in found, "非递归扫描不应进入子目录"


def test_scan_skips_trash_directory(sandbox: Path) -> None:
    trash = sandbox / filesystem.TRASH_DIRNAME
    trash.mkdir()
    (trash / "deleted.txt").write_text("x", encoding="utf-8")
    (sandbox / "keep.txt").write_text("x", encoding="utf-8")

    found = _names(filesystem.scan_directory(str(sandbox), recursive=True))
    assert "keep.txt" in found
    assert "deleted.txt" not in found and filesystem.TRASH_DIRNAME not in found


def test_scan_stops_at_item_cap(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """条目数达到上限即停止，不得把超大目录整份读进内存。"""
    monkeypatch.setattr(filesystem, "_MAX_SCAN_ITEMS", 10)
    for index in range(50):
        (sandbox / f"f{index:03d}.txt").write_text("x", encoding="utf-8")

    items = filesystem.scan_directory(str(sandbox), recursive=True)
    assert len(items) == 10, f"未按上限截断，收集了 {len(items)} 项"


def test_scan_stops_at_depth_cap(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超过深度上限的层级不再下探，浅层结果仍然完整。"""
    monkeypatch.setattr(filesystem, "_MAX_SCAN_DEPTH", 2)
    level1 = sandbox / "l1"
    level2 = level1 / "l2"
    level3 = level2 / "l3"
    level3.mkdir(parents=True)
    (sandbox / "top.txt").write_text("x", encoding="utf-8")
    (level1 / "one.txt").write_text("x", encoding="utf-8")
    (level3 / "three.txt").write_text("x", encoding="utf-8")

    found = _names(filesystem.scan_directory(str(sandbox), recursive=True))
    assert {"top.txt", "l1", "one.txt", "l2"} <= found
    assert "three.txt" not in found, "超过深度上限仍在下探"


def test_depth_cap_does_not_skip_shallow_sibling_branch(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(filesystem, "_MAX_SCAN_DEPTH", 2)
    shallow = sandbox / "a-shallow"
    shallow.mkdir()
    (shallow / "must-find.txt").write_text("x", encoding="utf-8")
    # 后压栈的分支先遍历，并在第二层碰到更深目录；旧实现会在这里终止整个扫描。
    (sandbox / "z-deep" / "child").mkdir(parents=True)

    found = _names(filesystem.scan_directory(str(sandbox), recursive=True))

    assert "must-find.txt" in found


def test_scan_does_not_follow_directory_symlink(sandbox: Path) -> None:
    """符号链接目录不跟进：指向自身的环不得让扫描停不下来。"""
    (sandbox / "real").mkdir()
    (sandbox / "real" / "file.txt").write_text("x", encoding="utf-8")
    try:
        # 指向沙箱根自身，构成目录环；旧实现会在此无限下探。
        os.symlink(str(sandbox), str(sandbox / "loop"), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("当前环境不允许创建符号链接（Windows 需开发者模式）")

    items = filesystem.scan_directory(str(sandbox), recursive=True)
    # 能返回结果本身就说明遍历终止了；关键是不得深入链接指向的目录。
    assert len(items) < 100, f"疑似跟进符号链接导致遍历膨胀：{len(items)} 项"


def _make_junction(link: Path, target: Path) -> bool:
    """用 mklink /J 建目录联接；失败返回 False（不需要管理员权限）。"""
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_scan_does_not_descend_into_junction(sandbox: Path) -> None:
    """Windows 目录联接既非符号链接又被判定为目录，必须单独识别。

    旧实现顺着联接会把同一批文件收集两次；若联接指向沙箱之外，还会越界枚举
    目录名。这里用指向兄弟目录的联接做确定性断言（不用环，避免测试挂起）。
    """
    real = sandbox / "real"
    real.mkdir()
    (real / "only.txt").write_text("x", encoding="utf-8")
    if not _make_junction(sandbox / "link", real):
        pytest.skip("当前环境无法创建目录联接")

    items = filesystem.scan_directory(str(sandbox), recursive=True)
    names = [item.name for item in items]
    assert names.count("only.txt") == 1, "顺着目录联接重复收集了内容"
    assert "link" in names, "联接自身作为条目仍应被收录"


def test_scan_terminates_on_junction_loop(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """联接指回沙箱根构成的环：必须终止且浅层结果完整。"""
    monkeypatch.setattr(filesystem, "_MAX_SCAN_DEPTH", 3)
    (sandbox / "real").mkdir()
    (sandbox / "real" / "f.txt").write_text("x", encoding="utf-8")
    if not _make_junction(sandbox / "loop", sandbox):
        pytest.skip("当前环境无法创建目录联接")

    items = filesystem.scan_directory(str(sandbox), recursive=True)
    assert {item.name for item in items} >= {"real", "f.txt", "loop"}


def test_scan_returns_metadata_shape(sandbox: Path) -> None:
    """FileMeta 字段仍按既有契约填充（上层分类与展示依赖）。"""
    target = sandbox / "报告.TXT"
    target.write_text("hello", encoding="utf-8")

    item = next(i for i in filesystem.scan_directory(str(sandbox)) if i.name == "报告.TXT")
    assert item.path.endswith("报告.TXT")
    assert item.ext == "txt", "扩展名应统一小写"
    assert item.size == 5
    assert item.is_dir is False
    assert item.modified.year >= 2000
