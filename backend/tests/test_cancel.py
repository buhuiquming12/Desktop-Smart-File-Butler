"""P1-4 回归：任务取消。执行循环在步骤边界检查中止请求并停止。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, main
from app.config import get_settings


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield TestClient(main.app)
    get_settings.cache_clear()
    db._initialized = False
    main._cancel_requested.clear()
    main._active_threads.clear()


def test_cancel_endpoint_marks_thread(client: TestClient) -> None:
    main._active_threads["abc"] = 1
    resp = client.post(
        "/api/threads/abc/cancel", headers={"X-Butler-Token": main.get_session_token()}
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "cancelling"
    assert "abc" in main._cancel_requested
    main._cancel_requested.discard("abc")
    main._active_threads.clear()


def test_cancel_non_running_thread_does_not_leave_stale_marker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _EmptyRuntime:
        def state(self, _thread_id: str):
            return {}

    monkeypatch.setattr(main, "get_runtime", lambda: _EmptyRuntime())
    resp = client.post(
        "/api/threads/never-started/cancel",
        headers={"X-Butler-Token": main.get_session_token()},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "not_running"
    assert "never-started" not in main._cancel_requested


def test_run_stream_stops_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """已请求中止时，即使迭代器是无限流，_run_stream 也会立即停止而不会挂死。"""
    tid = "cancel-me"

    class _FakeRuntime:
        def state(self, _tid: str):
            return {"status": "running"}

    monkeypatch.setattr(main, "get_runtime", lambda: _FakeRuntime())

    def _infinite():
        while True:
            yield ("updates", {})

    main._cancel_requested.add(tid)
    # 若中止检查失效，asyncio.run 会因无限迭代器而永不返回。
    result = asyncio.run(asyncio.wait_for(main._run_stream(_infinite(), tid, None), timeout=5))
    assert result == {"status": "running"}
    assert tid not in main._cancel_requested  # 已被消费清除


def test_cancel_resolves_persisted_pending_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = "cancel-pending"

    class _PendingRuntime:
        def __init__(self) -> None:
            self.value = {
                "status": "waiting_approval",
                "pending_approval": {"approval_id": "ap-1"},
                "observations": [],
            }
            self.decision = ""

        def state(self, _tid: str):
            return self.value

        def resume_stream(self, _tid: str, decision: str):
            self.decision = decision

            def updates():
                self.value = {"status": "running", "pending_approval": None, "observations": []}
                yield ("updates", {"approval": {"status": "running"}})

            return updates()

    runtime = _PendingRuntime()
    monkeypatch.setattr(main, "get_runtime", lambda: runtime)
    main._cancel_requested.add(tid)

    asyncio.run(main._resolve_cancelled_approval(tid))

    assert runtime.decision == "reject"
    assert runtime.value["pending_approval"] is None
    assert tid not in main._cancel_requested
