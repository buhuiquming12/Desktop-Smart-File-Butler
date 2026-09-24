"""P1-3 回归：默认管理目录必须落在授权目录内，且授权目录变更后自动失效。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db, sandbox_config, workspace_config
from app.agent.graph import AgentRuntime
from app.config import get_settings
from app.security import SandboxViolation, resolve_in_sandbox


@pytest.fixture()
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """授权目录 = A；另有 sandbox 外的 B，用来验证越界一律被拒。"""
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    downloads = root_a / "Downloads"
    for path in (root_a, root_b, downloads):
        path.mkdir(parents=True)
    monkeypatch.setenv("SANDBOX_ROOTS", str(root_a))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield root_a, root_b, downloads
    get_settings.cache_clear()
    db._initialized = False


def test_unconfigured_returns_none(dirs) -> None:
    assert workspace_config.effective_default_root() is None


def test_save_inside_sandbox(dirs) -> None:
    _, _, downloads = dirs
    saved = workspace_config.save_default_root(str(downloads))
    assert saved == downloads.resolve()
    assert workspace_config.effective_default_root() == downloads.resolve()


def test_reject_outside_sandbox(dirs) -> None:
    _, root_b, _ = dirs
    with pytest.raises(workspace_config.InvalidDefaultRoot) as excinfo:
        workspace_config.save_default_root(str(root_b))
    assert "授权目录" in str(excinfo.value)
    assert workspace_config.effective_default_root() is None
    assert db.get_preference(workspace_config.DEFAULT_ROOT_KEY) is None


def test_reject_missing_directory(dirs) -> None:
    root_a, _, _ = dirs
    with pytest.raises(workspace_config.InvalidDefaultRoot):
        workspace_config.save_default_root(str(root_a / "not-created-yet"))


def test_reject_file_as_root(dirs) -> None:
    root_a, _, _ = dirs
    some_file = root_a / "a.txt"
    some_file.write_text("x", encoding="utf-8")
    with pytest.raises(workspace_config.InvalidDefaultRoot):
        workspace_config.save_default_root(str(some_file))


def test_clear_falls_back_to_none(dirs) -> None:
    _, _, downloads = dirs
    workspace_config.save_default_root(str(downloads))
    workspace_config.save_default_root("")   # 空串 = 清除
    assert workspace_config.effective_default_root() is None


def test_sandbox_shrink_prunes_default_root(dirs) -> None:
    """授权目录收紧导致默认目录越界：必须清除，不能让它继续生效。"""
    root_a, root_b, downloads = dirs
    workspace_config.save_default_root(str(downloads))
    assert workspace_config.effective_default_root() == downloads.resolve()

    sandbox_config.save_roots([str(root_b)])  # 只剩 B，Downloads 已越界
    assert workspace_config.effective_default_root() is None
    assert db.get_preference(workspace_config.DEFAULT_ROOT_KEY) == ""


def test_out_of_bounds_value_is_ignored_even_if_stored(dirs) -> None:
    """绕过界面写入（手改数据库 / 旧版本遗留）也不能生效：读取侧同样有校验。"""
    _, root_b, _ = dirs
    db.set_preference(
        workspace_config.DEFAULT_ROOT_KEY, str(root_b), allow_reserved=True
    )
    assert workspace_config.stored_default_root() == root_b.resolve()
    assert workspace_config.effective_default_root() is None


def test_deleted_directory_is_ignored(dirs) -> None:
    _, _, downloads = dirs
    workspace_config.save_default_root(str(downloads))
    downloads.rmdir()
    assert workspace_config.effective_default_root() is None


def test_reserved_key_hidden_from_preferences(dirs) -> None:
    _, _, downloads = dirs
    workspace_config.save_default_root(str(downloads))
    assert workspace_config.DEFAULT_ROOT_KEY not in {p.key for p in db.all_preferences()}


def test_agent_cannot_write_reserved_default_root_key(dirs) -> None:
    _, root_b, _ = dirs
    runtime = object.__new__(AgentRuntime)
    result = runtime._execute_step({
        "id": "reserved-workspace",
        "description": "不得修改默认管理目录",
        "tool": "set_preference",
        "args": {"key": workspace_config.DEFAULT_ROOT_KEY, "value": str(root_b)},
    })

    assert result["status"] == "failed"
    assert workspace_config.effective_default_root() is None


def test_perception_exposes_default_managed_root(dirs) -> None:
    root_a, _, downloads = dirs
    runtime = object.__new__(AgentRuntime)
    initial = {
        "thread_id": "t-workspace",
        "user_request": "整理一下文件",
        "mutations": {"move": 0, "rename": 0, "delete": 0},
    }

    empty = runtime._perceive(dict(initial))
    assert empty["perception"]["default_managed_root"] == ""
    assert empty["perception"]["allowed_roots"] == [str(root_a.resolve())]

    workspace_config.save_default_root(str(downloads))
    filled = runtime._perceive(dict(initial))
    assert filled["perception"]["default_managed_root"] == str(downloads.resolve())


def test_default_root_is_not_a_permission_shortcut(dirs) -> None:
    """默认目录只是默认位置：越界路径仍然被 sandbox 拦下（不因它而放行）。"""
    _, root_b, _ = dirs
    outside = root_b / "b.txt"
    outside.write_text("b", encoding="utf-8")
    with pytest.raises(SandboxViolation):
        resolve_in_sandbox(str(outside), must_exist=True)
