"""P1 回归：大小分组 + 内容哈希、硬链接、跳过回收站/链接、变化失效与清理核验。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.main import app, get_session_token
from app.tools import duplicates


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    media = root / "media"
    media.mkdir(parents=True)
    monkeypatch.setenv("SANDBOX_ROOTS", str(root))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    client = TestClient(app)
    client.headers.update({"X-Butler-Token": get_session_token()})
    yield SimpleEnv(root, media, client)
    get_settings.cache_clear()
    db._initialized = False


class SimpleEnv:
    def __init__(self, root: Path, media: Path, client: TestClient) -> None:
        self.root = root
        self.media = media
        self.client = client

    def write(self, name: str, content: str, *, directory: Path | None = None) -> Path:
        path = (directory or self.media) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def scan(self, **body) -> dict:
        response = self.client.post(
            "/api/duplicates/scan",
            json={"directories": [str(self.media)], **body},
        )
        assert response.status_code == 202, response.text
        scan_id = response.json()["scan_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            state = self.client.get(f"/api/duplicates/{scan_id}").json()
            if state["status"] != "running":
                return state
            time.sleep(0.05)
        raise AssertionError("重复文件扫描超时")


def test_same_size_different_content_is_not_duplicate(env: SimpleEnv) -> None:
    env.write("a.txt", "abcdef")
    env.write("b.txt", "abcdeg")
    state = env.scan()
    assert state["summary"]["duplicate_groups"] == 0
    assert state["groups"] == []


def test_same_content_different_names_forms_group(env: SimpleEnv) -> None:
    old = env.write("old.txt", "identical-content")
    new = env.write("new.txt", "identical-content")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    state = env.scan()
    assert state["summary"]["duplicate_groups"] == 1
    group = state["groups"][0]
    assert group["count"] == 2
    assert group["reclaimable"] == len("identical-content")
    # 保留建议取修改时间最新的一份，并说明理由。
    assert group["suggested_keep"].endswith("new.txt")
    assert "修改时间最新" in group["keep_reason"]
    assert sum(1 for item in group["files"] if item["delete_by_default"]) == 1


def test_trash_directory_is_skipped(env: SimpleEnv) -> None:
    env.write("a.txt", "same")
    env.write("b.txt", "same")
    env.write("a.txt", "same", directory=env.root / ".butler-trash")
    state = env.scan()
    paths = [item["path"] for group in state["groups"] for item in group["files"]]
    assert all(".butler-trash" not in path for path in paths)


def test_hardlinks_do_not_inflate_reclaimable(env: SimpleEnv) -> None:
    first = env.write("a.txt", "hardlink-content")
    second = env.media / "b.txt"
    try:
        os.link(first, second)
    except (OSError, NotImplementedError):
        pytest.skip("当前文件系统不支持硬链接")
    state = env.scan()
    assert state["summary"]["duplicate_groups"] == 1
    group = state["groups"][0]
    assert group["count"] == 2
    assert group["distinct_inodes"] == 1
    assert group["reclaimable"] == 0
    assert any(item["hardlink_of"] for item in group["files"])


def test_symlinked_file_is_skipped(env: SimpleEnv, tmp_path: Path) -> None:
    target = env.write("real.txt", "content-for-link")
    link = env.media / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接")
    state = env.scan()
    paths = [item["path"] for group in state["groups"] for item in group["files"]]
    assert str(link) not in paths


def test_hash_stable_marks_file_changed_while_hashing(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    path = tmp_path / "changing.txt"
    path.write_text("first", encoding="utf-8")
    entry = {"path": str(path), "size": 5, "mtime_ns": path.stat().st_mtime_ns}
    calls = {"count": 0}

    def fake_hash(_path, **_kwargs):
        calls["count"] += 1
        with path.open("a", encoding="utf-8") as handle:
            handle.write("x")
        return f"hash-{calls['count']}"

    monkeypatch.setattr(duplicates, "_hash_file", fake_hash)
    result = duplicates._hash_stable(entry, threading.Event(), {"bytes_read": 0})
    assert result is None
    assert calls["count"] == 2


def test_cleanup_reverifies_content_and_uses_trash(env: SimpleEnv) -> None:
    keeper = env.write("keep.txt", "same-content")
    victim = env.write("victim.txt", "same-content")
    os.utime(keeper, (1_700_000_000, 1_700_000_000))
    os.utime(victim, (1_600_000_000, 1_600_000_000))
    state = env.scan()
    group = state["groups"][0]
    assert group["suggested_keep"] == str(keeper)

    # 扫描后保留项内容变化：不应再删除另一份（内容已不同）。
    keeper.write_text("different-now", encoding="utf-8")
    response = env.client.post(
        f"/api/duplicates/{state['scan_id']}/cleanup", json={"confirm": True}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == 0
    assert body["skipped"] == 1
    assert victim.exists()

    # 恢复一致后清理：被删项进入回收站，可按线程回滚。
    keeper.write_text("same-content", encoding="utf-8")
    again = env.scan()
    response = env.client.post(
        f"/api/duplicates/{again['scan_id']}/cleanup",
        json={"confirm": True, "keep_map": {again["groups"][0]["group_id"]: str(keeper)}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] == 1
    assert not victim.exists()
    trash = list((env.root / ".butler-trash").glob("victim*.txt"))
    assert trash
    assert body["thread_id"].startswith("dedupe-")


def test_cleanup_requires_confirmation(env: SimpleEnv) -> None:
    env.write("a.txt", "same")
    env.write("b.txt", "same")
    state = env.scan()
    response = env.client.post(f"/api/duplicates/{state['scan_id']}/cleanup", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "confirm_required"
    assert (env.media / "a.txt").exists() and (env.media / "b.txt").exists()


def test_scan_outside_sandbox_is_rejected(env: SimpleEnv, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    response = env.client.post(
        "/api/duplicates/scan", json={"directories": [str(outside)]}
    )
    assert response.status_code == 422
