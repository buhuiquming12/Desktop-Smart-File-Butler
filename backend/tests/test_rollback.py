"""P1-1 回归：撤销 / 回滚，删除走回收站，扫描跳过回收站。"""
from __future__ import annotations

from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app import db, main
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


def test_move_then_rollback_returns_to_origin(sandbox: Path) -> None:
    source = sandbox / "report.txt"
    source.write_text("hello", encoding="utf-8")
    dest_dir = sandbox / "docs"

    moved = Path(filesystem.move_file(str(source), str(dest_dir)))
    assert moved.exists() and not source.exists()

    op = db.recent_operations(1)[0]
    assert op.action == "move"
    result = filesystem.restore_operation(op)

    assert result["status"] == "ok"
    assert source.exists() and source.read_text(encoding="utf-8") == "hello"
    assert not moved.exists()


def test_delete_moves_to_trash_then_rollback_restores(sandbox: Path) -> None:
    victim = sandbox / "secret.txt"
    victim.write_text("keep me", encoding="utf-8")

    trashed = Path(filesystem.delete_file(str(victim)))
    # 删除不是物理删除：原文件消失，回收站里出现副本。
    assert not victim.exists()
    assert trashed.exists()
    assert filesystem.TRASH_DIRNAME in trashed.parts

    op = db.recent_operations(1)[0]
    assert op.action == "delete" and op.status == "ok"
    result = filesystem.restore_operation(op)

    assert result["status"] == "ok"
    assert victim.exists() and victim.read_text(encoding="utf-8") == "keep me"
    assert not trashed.exists()


def test_scan_skips_trash(sandbox: Path) -> None:
    keep = sandbox / "keep.txt"
    keep.write_text("k", encoding="utf-8")
    victim = sandbox / "gone.txt"
    victim.write_text("g", encoding="utf-8")
    filesystem.delete_file(str(victim))

    names = {f.name for f in filesystem.scan_directory(str(sandbox), recursive=True)}
    assert "keep.txt" in names
    assert "gone.txt" not in names
    assert filesystem.TRASH_DIRNAME not in names


def test_rollback_to_occupied_origin_uses_unique_name(sandbox: Path) -> None:
    source = sandbox / "dup.txt"
    source.write_text("original", encoding="utf-8")
    dest_dir = sandbox / "sub"
    moved = Path(filesystem.move_file(str(source), str(dest_dir)))
    # 原位置又被新文件占用
    source.write_text("new occupant", encoding="utf-8")

    op = db.recent_operations(1)[0]
    result = filesystem.restore_operation(op)

    assert result["status"] == "ok"
    assert source.read_text(encoding="utf-8") == "new occupant"  # 未被覆盖
    restored = Path(result["restored_to"])
    assert restored.exists() and restored != source
    assert restored.read_text(encoding="utf-8") == "original"


def test_thread_grouping_enables_thread_rollback(sandbox: Path) -> None:
    dest_dir = sandbox / "archive"
    moved_paths = []
    with db.operation_thread("thread-A"):
        for i in range(3):
            f = sandbox / f"f{i}.txt"
            f.write_text(str(i), encoding="utf-8")
            moved_paths.append(Path(filesystem.move_file(str(f), str(dest_dir))))
    # 另一个会话的操作不应被卷入
    other = sandbox / "other.txt"
    other.write_text("x", encoding="utf-8")
    with db.operation_thread("thread-B"):
        filesystem.move_file(str(other), str(dest_dir))

    ops = db.operations_for_thread("thread-A")
    assert len(ops) == 3 and all(o.thread_id == "thread-A" for o in ops)

    for op in reversed(ops):
        assert filesystem.restore_operation(op)["status"] == "ok"
    for i in range(3):
        assert (sandbox / f"f{i}.txt").exists()


# ---------------- HTTP 端点 ----------------

def _auth() -> dict:
    return {"X-Butler-Token": main.get_session_token()}


def test_rollback_operation_endpoint(sandbox: Path) -> None:
    client = TestClient(main.app)
    source = sandbox / "e.txt"
    source.write_text("api", encoding="utf-8")
    moved = Path(filesystem.move_file(str(source), str(sandbox / "d")))
    op_id = db.recent_operations(1)[0].id

    resp = client.post(f"/api/operations/{op_id}/rollback", headers=_auth())
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert source.exists() and not moved.exists()


def test_rollback_operation_endpoint_requires_token(sandbox: Path) -> None:
    client = TestClient(main.app)
    resp = client.post("/api/operations/1/rollback")
    assert resp.status_code == 401


def test_rollback_thread_endpoint(sandbox: Path) -> None:
    client = TestClient(main.app)
    dest_dir = sandbox / "arch"
    with db.operation_thread("thread-http"):
        for i in range(2):
            f = sandbox / f"h{i}.txt"
            f.write_text(str(i), encoding="utf-8")
            filesystem.move_file(str(f), str(dest_dir))

    resp = client.post("/api/threads/thread-http/rollback", headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] == 2 and body["total"] == 2
    assert (sandbox / "h0.txt").exists() and (sandbox / "h1.txt").exists()
