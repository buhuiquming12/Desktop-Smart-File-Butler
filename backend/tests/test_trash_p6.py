"""P6 回归：回收站列表与恢复（含冲突不覆盖、逐项结果）。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.main import app, get_session_token
from app.tools import filesystem


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    docs = root / "docs"
    docs.mkdir(parents=True)
    monkeypatch.setenv("SANDBOX_ROOTS", str(root))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    client = TestClient(app)
    client.headers.update({"X-Butler-Token": get_session_token()})
    yield SimpleEnv(root, docs, client)
    get_settings.cache_clear()
    db._initialized = False


class SimpleEnv:
    def __init__(self, root: Path, docs: Path, client: TestClient) -> None:
        self.root = root
        self.docs = docs
        self.client = client

    def delete(self, name: str, content: str = "payload", thread: str = "thread-1") -> int:
        path = self.docs / name
        path.write_text(content, encoding="utf-8")
        with db.operation_thread(thread):
            filesystem.delete_file(str(path))
        return max(item.id or 0 for item in db.recent_operations(20))

    def list(self, **params) -> dict:
        response = self.client.get("/api/trash", params=params)
        assert response.status_code == 200, response.text
        return response.json()


def test_list_shows_metadata_and_task(env: SimpleEnv) -> None:
    op_id = env.delete("report.pdf", "hello", thread="preview-abc")
    body = env.list()
    assert body["total"] == 1
    entry = body["entries"][0]
    assert entry["op_id"] == op_id
    assert entry["name"] == "report.pdf"
    assert Path(entry["original_path"]).name == "report.pdf"
    assert entry["size"] == len("hello")
    assert entry["task"] == "操作预览"
    assert entry["deleted_at"]
    assert entry["conflict"] is False


def test_restore_returns_file(env: SimpleEnv) -> None:
    env.delete("a.txt", "content-a")
    body = env.list()
    op_id = body["entries"][0]["op_id"]
    response = env.client.post("/api/trash/restore", json={"op_ids": [op_id]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] == 1 and result["failed"] == 0
    assert (env.docs / "a.txt").read_text(encoding="utf-8") == "content-a"
    assert env.list()["total"] == 0


def test_restore_conflict_never_overwrites(env: SimpleEnv) -> None:
    op_id = env.delete("a.txt", "old-content")
    (env.docs / "a.txt").write_text("new-content", encoding="utf-8")
    body = env.list()
    assert body["entries"][0]["conflict"] is True
    result = env.client.post("/api/trash/restore", json={"op_ids": [op_id]}).json()
    assert result["ok"] == 1
    assert (env.docs / "a.txt").read_text(encoding="utf-8") == "new-content"
    restored = Path(result["results"][0]["restored_to"])
    assert restored.name.startswith("a")
    assert restored.read_text(encoding="utf-8") == "old-content"
    assert result["results"][0]["conflict"] is True


def test_partial_restore_reports_each_item(env: SimpleEnv) -> None:
    first = env.delete("x.txt")
    second = env.delete("y.txt")
    # 手工移除回收站里的第二份，模拟已被外部清理。
    trash = env.root / ".butler-trash"
    for path in trash.glob("y*.txt"):
        path.unlink()
    result = env.client.post("/api/trash/restore", json={"op_ids": [first, second]}).json()
    assert result["ok"] == 1
    assert result["skipped"] == 1
    assert {item["op_id"] for item in result["results"]} == {first, second}
    statuses = {item["op_id"]: item["status"] for item in result["results"]}
    assert statuses[first] == "ok" and statuses[second] == "skipped"


def test_filter_by_name(env: SimpleEnv) -> None:
    env.delete("alpha.txt")
    env.delete("beta.txt")
    filtered = env.list(query="alpha")
    assert filtered["total"] == 1
    assert filtered["entries"][0]["name"] == "alpha.txt"


def test_restore_unknown_operation(env: SimpleEnv) -> None:
    result = env.client.post("/api/trash/restore", json={"op_ids": [99999]}).json()
    assert result["failed"] == 1
    assert "不存在" in result["results"][0]["detail"]


def test_restored_entry_disappears_from_list(env: SimpleEnv) -> None:
    env.delete("gone.txt")
    entry = env.list()["entries"][0]
    filesystem.restore_operation(db.get_operation(entry["op_id"]))
    assert env.list()["total"] == 0
