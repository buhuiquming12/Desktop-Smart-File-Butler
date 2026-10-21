"""P0 回归：操作预览的只读性、选择、确认绑定与失效重检。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.main import app, get_session_token


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    source = root / "inbox"
    source.mkdir(parents=True)
    monkeypatch.setenv("SANDBOX_ROOTS", str(root))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    client = TestClient(app)
    client.headers.update({"X-Butler-Token": get_session_token()})
    yield SimpleEnv(root, source, client)
    get_settings.cache_clear()
    db._initialized = False


class SimpleEnv:
    def __init__(self, root: Path, source: Path, client: TestClient) -> None:
        self.root = root
        self.source = source
        self.client = client

    def make(self, name: str, content: str = "x") -> Path:
        path = self.source / name
        path.write_text(content, encoding="utf-8")
        return path

    def preview(self, **body):
        payload = {"op": "move", "directory": str(self.source), "dest_dir": str(self.root / "out"), **body}
        response = self.client.post("/api/previews", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    def ready_ids(self, preview: dict) -> list[str]:
        return [item["item_id"] for item in preview["items"] if item["status"] == "ready"]


def test_preview_is_read_only(env: SimpleEnv) -> None:
    env.make("a.pdf")
    env.make("b.jpg")
    preview = env.preview(filter={"extensions": ["pdf"]})
    assert preview["counts"]["total"] == 1
    assert not (env.root / "out").exists()
    assert (env.source / "a.pdf").exists()
    item = preview["items"][0]
    assert item["op"] == "move"
    assert item["source"].endswith("a.pdf")
    assert item["target"].endswith("a.pdf")
    assert item["status"] == "ready"


def test_execute_only_selected_items(env: SimpleEnv) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        env.make(name)
    preview = env.preview()
    selected = env.ready_ids(preview)[:2]
    confirm = env.client.post(f"/api/previews/{preview['preview_id']}/confirm", json={"item_ids": selected})
    assert confirm.status_code == 200, confirm.text
    token = confirm.json()["approval_id"]
    result = env.client.post(
        f"/api/previews/{preview['preview_id']}/execute",
        json={"item_ids": selected, "confirm_token": token},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["selected"] == 2 and body["ok"] == 2
    assert len(list((env.root / "out").glob("*.txt"))) == 2
    assert len(list(env.source.glob("*.txt"))) == 1


def test_changing_selection_after_confirm_is_rejected(env: SimpleEnv) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        env.make(name)
    preview = env.preview()
    ids = env.ready_ids(preview)
    token = env.client.post(
        f"/api/previews/{preview['preview_id']}/confirm", json={"item_ids": ids[:2]}
    ).json()["approval_id"]
    response = env.client.post(
        f"/api/previews/{preview['preview_id']}/execute",
        json={"item_ids": ids, "confirm_token": token},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "selection_changed"
    # 一个文件都没有被移动
    assert not (env.root / "out").exists()


def test_execute_without_confirm_token_is_rejected(env: SimpleEnv) -> None:
    env.make("a.txt")
    preview = env.preview()
    response = env.client.post(
        f"/api/previews/{preview['preview_id']}/execute",
        json={"item_ids": env.ready_ids(preview), "confirm_token": "forged"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "confirm_required"


def test_changed_source_requires_refresh(env: SimpleEnv) -> None:
    path = env.make("a.txt", "v1")
    preview = env.preview()
    ids = env.ready_ids(preview)
    token = env.client.post(
        f"/api/previews/{preview['preview_id']}/confirm", json={"item_ids": ids}
    ).json()["approval_id"]
    path.write_text("v2-new-content", encoding="utf-8")
    response = env.client.post(
        f"/api/previews/{preview['preview_id']}/execute",
        json={"item_ids": ids, "confirm_token": token},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "stale_preview"
    assert detail["stale"][0]["source"].endswith("a.txt")
    assert not (env.root / "out").exists()


def test_new_conflict_at_target_blocks_execution(env: SimpleEnv) -> None:
    env.make("a.txt")
    preview = env.preview()
    ids = env.ready_ids(preview)
    token = env.client.post(
        f"/api/previews/{preview['preview_id']}/confirm", json={"item_ids": ids}
    ).json()["approval_id"]
    out = env.root / "out"
    out.mkdir()
    (out / "a.txt").write_text("occupied", encoding="utf-8")
    response = env.client.post(
        f"/api/previews/{preview['preview_id']}/execute",
        json={"item_ids": ids, "confirm_token": token},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_preview"
    assert (out / "a.txt").read_text(encoding="utf-8") == "occupied"


def test_delete_preview_requires_explicit_approval(env: SimpleEnv) -> None:
    env.make("a.txt")
    preview = env.preview(op="delete", dest_dir="")
    ids = env.ready_ids(preview)
    assert preview["items"][0]["target"] == "（项目回收站）"
    rejected = env.client.post(
        f"/api/previews/{preview['preview_id']}/confirm", json={"item_ids": ids}
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "approval_required"
    approved = env.client.post(
        f"/api/previews/{preview['preview_id']}/confirm",
        json={"item_ids": ids, "approve": True},
    )
    assert approved.status_code == 200
    result = env.client.post(
        f"/api/previews/{preview['preview_id']}/execute",
        json={"item_ids": ids, "confirm_token": approved.json()["approval_id"]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["ok"] == 1
    assert not (env.source / "a.txt").exists()
    # 回收站位于沙箱根目录下（filesystem.delete_file 的既有约定）。
    assert (env.root / ".butler-trash" / "a.txt").exists()


def test_threshold_requires_approval_flag(env: SimpleEnv) -> None:
    db.set_preference("batch_approval_threshold", "1")
    env.make("a.txt")
    env.make("b.txt")
    preview = env.preview()
    ids = env.ready_ids(preview)
    response = env.client.post(
        f"/api/previews/{preview['preview_id']}/confirm", json={"item_ids": ids}
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["requires_approval"] is True
    assert detail["count"] == 2


def test_preview_pagination(env: SimpleEnv) -> None:
    for index in range(7):
        env.make(f"f{index}.txt")
    preview = env.preview()
    page = env.client.get(f"/api/previews/{preview['preview_id']}?limit=3&offset=0").json()
    assert len(page["items"]) == 3 and page["has_more"] is True
    last = env.client.get(f"/api/previews/{preview['preview_id']}?limit=3&offset=6").json()
    assert len(last["items"]) == 1 and last["has_more"] is False


def test_conflict_items_are_not_selectable(env: SimpleEnv) -> None:
    env.make("a.txt", "one")
    out = env.root / "out"
    out.mkdir()
    (out / "a.txt").write_text("occupied", encoding="utf-8")
    preview = env.preview(filter={"extensions": ["txt"]})
    item = preview["items"][0]
    assert item["status"] == "conflict"
    assert "不会覆盖" in item["reason"]
    response = env.client.post(
        f"/api/previews/{preview['preview_id']}/confirm", json={"item_ids": [item["item_id"]]}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_ready"
