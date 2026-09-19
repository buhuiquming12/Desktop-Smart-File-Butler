"""P1-3 回归：沙箱根目录 DB 覆盖 .env，改配置后立即生效。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db, sandbox_config
from app.config import get_settings
from app.security import SandboxViolation, resolve_in_sandbox


@pytest.fixture()
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "a.txt").write_text("a", encoding="utf-8")
    (root_b / "b.txt").write_text("b", encoding="utf-8")
    monkeypatch.setenv("SANDBOX_ROOTS", str(root_a))  # .env 默认只允许 A
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield root_a, root_b
    get_settings.cache_clear()
    db._initialized = False


def test_override_switches_roots_immediately(dirs) -> None:
    root_a, root_b = dirs
    # 初始：A 可访问，B 被拒
    assert resolve_in_sandbox(str(root_a / "a.txt"), must_exist=True)
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(str(root_b / "b.txt"), must_exist=True)

    # 覆盖为 B：立即生效，B 可访问，A 被拒
    sandbox_config.save_roots([str(root_b)])
    assert resolve_in_sandbox(str(root_b / "b.txt"), must_exist=True)
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(str(root_a / "a.txt"), must_exist=True)


def test_clear_override_falls_back_to_env(dirs) -> None:
    root_a, root_b = dirs
    sandbox_config.save_roots([str(root_b)])
    sandbox_config.save_roots([])  # 清除覆盖
    assert resolve_in_sandbox(str(root_a / "a.txt"), must_exist=True)
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(str(root_b / "b.txt"), must_exist=True)


def test_reject_non_directory(dirs) -> None:
    root_a, _ = dirs
    with pytest.raises(sandbox_config.InvalidSandboxRoot):
        sandbox_config.save_roots([str(root_a / "does-not-exist")])


def test_reserved_key_hidden_from_preferences(dirs) -> None:
    _, root_b = dirs
    sandbox_config.save_roots([str(root_b)])
    keys = {p.key for p in db.all_preferences()}
    assert sandbox_config.ROOTS_KEY not in keys
