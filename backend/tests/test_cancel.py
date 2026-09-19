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
    main._cancel_requested.discard("cancel-me")


def test_cancel_endpoint_marks_thread(client: TestClient) -> None:
    resp = client.post(
        "/api/threads/abc/cancel", headers={"X-Butler-Token": main.get_session_token()}
    )
    assert resp.status_code == 202
    assert "abc" in main._cancel_requested
    main._cancel_requested.discard("abc")


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
