"""P0-2 回归：REST 与 WebSocket 的会话令牌 + 本机来源校验。

覆盖：无令牌 / 错误令牌 / 恶意来源被拒；正确令牌通过；/api/health 免令牌。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import db, main
from app.config import get_settings

GOOD = main.get_session_token()
BAD = "wrong-token"
EVIL_ORIGIN = "https://evil.example.com"
LOCAL_ORIGIN = "http://127.0.0.1:5173"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield TestClient(main.app)
    get_settings.cache_clear()
    db._initialized = False


# ---------------- REST ----------------

def test_rest_missing_token_rejected(client: TestClient) -> None:
    resp = client.get("/api/preferences", headers={"origin": LOCAL_ORIGIN})
    assert resp.status_code == 401


def test_rest_wrong_token_rejected(client: TestClient) -> None:
    resp = client.get(
        "/api/preferences",
        headers={"origin": LOCAL_ORIGIN, "X-Butler-Token": BAD},
    )
    assert resp.status_code == 401


def test_rest_evil_origin_rejected_even_with_token(client: TestClient) -> None:
    resp = client.get(
        "/api/preferences",
        headers={"origin": EVIL_ORIGIN, "X-Butler-Token": GOOD},
    )
    assert resp.status_code == 403


def test_rest_null_origin_rejected(client: TestClient) -> None:
    resp = client.get(
        "/api/preferences",
        headers={"origin": "null", "X-Butler-Token": GOOD},
    )
    assert resp.status_code == 403


def test_rest_valid_token_allowed(client: TestClient) -> None:
    resp = client.get(
        "/api/preferences",
        headers={"origin": LOCAL_ORIGIN, "X-Butler-Token": GOOD},
    )
    assert resp.status_code == 200


def test_health_exempt_from_token(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


def test_settings_llm_put_requires_token(client: TestClient) -> None:
    """密钥外泄链的入口：改 base_url 必须先过令牌校验。"""
    resp = client.put(
        "/api/settings/llm",
        json={"openai_base_url": "http://attacker.example.com/v1"},
        headers={"origin": EVIL_ORIGIN},
    )
    assert resp.status_code in (401, 403)


# ---------------- WebSocket ----------------

def test_ws_missing_token_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/abc"):
            pass


def test_ws_wrong_token_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/abc?token={BAD}"):
            pass


def test_ws_evil_origin_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/ws/abc?token={GOOD}", headers={"origin": EVIL_ORIGIN}
        ):
            pass


def test_ws_valid_token_accepted(client: TestClient) -> None:
    with client.websocket_connect(f"/ws/abc?token={GOOD}") as ws:
        event = ws.receive_json()
        assert event["type"] == "node"
        assert event["payload"]["node"] == "connected"
