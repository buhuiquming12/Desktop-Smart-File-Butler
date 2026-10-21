"""P5 回归：规则 CRUD、写入稳定判定、去重、重启补扫、自循环与阈值保护。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.main import app, get_session_token
from app.tools import watcher


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    inbox = root / "inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("SANDBOX_ROOTS", str(root))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    client = TestClient(app)
    client.headers.update({"X-Butler-Token": get_session_token()})
    yield SimpleEnv(root, inbox, client)
    get_settings.cache_clear()
    db._initialized = False


class SimpleEnv:
    def __init__(self, root: Path, inbox: Path, client: TestClient) -> None:
        self.root = root
        self.inbox = inbox
        self.client = client

    def write(self, name: str, content: str = "data") -> Path:
        path = self.inbox / name
        path.write_text(content, encoding="utf-8")
        return path

    def create_rule(self, **body) -> dict:
        payload = {
            "directory": str(self.inbox),
            "dest_root": str(self.root / "sorted"),
            "mode": "extension",
            "settle_seconds": 1,
            **body,
        }
        response = self.client.post("/api/rules", json=payload)
        assert response.status_code == 201, response.text
        return response.json()


def test_rule_crud_and_lifecycle(env: SimpleEnv) -> None:
    rule = env.create_rule()
    assert rule["enabled"] is True
    listed = env.client.get("/api/rules").json()
    assert len(listed["rules"]) == 1
    assert isinstance(listed["running"], bool)

    disabled = env.client.put(f"/api/rules/{rule['rule_id']}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    deleted = env.client.delete(f"/api/rules/{rule['rule_id']}")
    assert deleted.status_code == 200
    assert env.client.get("/api/rules").json()["rules"] == []


def test_rule_rejects_same_source_and_destination(env: SimpleEnv) -> None:
    response = env.client.post("/api/rules", json={
        "directory": str(env.inbox),
        "dest_root": str(env.inbox),
    })
    assert response.status_code == 422
    assert "自我移动" in response.json()["detail"]


def test_waits_for_file_to_stabilize(env: SimpleEnv) -> None:
    env.write("a.txt")
    rule = env.create_rule(settle_seconds=5)
    engine = watcher.RuleWatcher(poll_interval=0.2)
    # 第一次观察只记录签名，不处理。
    first = engine.poll_rule(rule, now=100.0)
    assert first["processed"] == 0
    assert (env.inbox / "a.txt").exists()
    # 未达到稳定时长仍不处理。
    second = engine.poll_rule(rule, now=102.0)
    assert second["processed"] == 0
    # 超过稳定时长后处理。
    third = engine.poll_rule(rule, now=106.0)
    assert third["processed"] == 1
    assert not (env.inbox / "a.txt").exists()
    assert (env.root / "sorted" / "文档" / "a.txt").exists()


def test_written_file_resets_stability_timer(env: SimpleEnv) -> None:
    path = env.write("growing.txt", "part1")
    rule = env.create_rule(settle_seconds=3)
    engine = watcher.RuleWatcher(poll_interval=0.2)
    assert engine.poll_rule(rule, now=10.0)["processed"] == 0
    time.sleep(0.01)
    path.write_text("part1-and-more", encoding="utf-8")
    # 签名变化 → 重新计时，不处理。
    assert engine.poll_rule(rule, now=12.0)["processed"] == 0
    assert engine.poll_rule(rule, now=15.0)["processed"] == 1


def test_no_duplicate_processing_and_restart_catch_up(env: SimpleEnv) -> None:
    env.write("a.txt")
    rule = env.create_rule()
    assert watcher.run_rule_once(rule["rule_id"])["processed"] == 1
    # 已处理事件持久化：再次触发（模拟重启）不会重复搬同一个文件。
    assert watcher.run_rule_once(rule["rule_id"])["processed"] == 0
    # 重启后新到达的文件仍会被补上。
    env.write("b.txt")
    assert watcher.run_rule_once(rule["rule_id"])["processed"] == 1
    assert (env.root / "sorted" / "文档" / "b.txt").exists()


def test_destination_subtree_is_not_reprocessed(env: SimpleEnv) -> None:
    dest = env.inbox / "sorted"
    rule = env.create_rule(dest_root=str(dest))
    env.write("a.txt")
    assert watcher.run_rule_once(rule["rule_id"])["processed"] == 1
    moved = dest / "文档" / "a.txt"
    assert moved.exists()
    # 目标目录在监听目录内，但必须被跳过，不能形成自我移动循环。
    assert watcher.run_rule_once(rule["rule_id"])["processed"] == 0
    assert moved.exists()


def test_batch_over_threshold_is_not_executed(env: SimpleEnv) -> None:
    db.set_preference("batch_approval_threshold", "1")
    for name in ("a.txt", "b.txt"):
        env.write(name)
    rule = env.create_rule()
    result = watcher.run_rule_once(rule["rule_id"])
    assert result["processed"] == 0
    assert result["skipped"] == 2
    assert "超过自动处理上限" in result["reason"]
    assert (env.inbox / "a.txt").exists() and (env.inbox / "b.txt").exists()
    stored = env.client.get("/api/rules").json()["rules"][0]
    assert "超过自动处理上限" in stored["last_result"]
    # 已记录跳过事件，不会每轮重复刷日志。
    assert watcher.run_rule_once(rule["rule_id"])["skipped"] == 0


def test_content_mode_falls_back_when_model_missing(env: SimpleEnv) -> None:
    env.write("photo.jpg", "fake")
    rule = env.create_rule(mode="content")
    assert watcher.run_rule_once(rule["rule_id"])["processed"] == 1
    assert (env.root / "sorted" / "图片" / "photo.jpg").exists()


def test_temp_files_are_ignored(env: SimpleEnv) -> None:
    env.write("download.crdownload")
    env.write("~$draft.docx")
    rule = env.create_rule()
    assert watcher.run_rule_once(rule["rule_id"])["processed"] == 0
    assert (env.inbox / "download.crdownload").exists()


def test_watcher_thread_start_stop() -> None:
    engine = watcher.start_watcher(poll_interval=60)
    try:
        assert watcher.watcher_running() is True
    finally:
        watcher.stop_watcher()
    assert watcher.watcher_running() is False


def test_rule_operations_are_audited(env: SimpleEnv) -> None:
    env.write("a.txt")
    rule = env.create_rule()
    watcher.run_rule_once(rule["rule_id"])
    operations = env.client.get("/api/operations").json()
    moved = [item for item in operations if item["action"] == "move"]
    assert moved and moved[0]["thread_id"] == f"rule-{rule['rule_id']}"
