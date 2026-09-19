"""P0-1 回归：前端由后端 StaticFiles 同源托管，且不遮挡 API / WS 路由。

打包模式下前后端同源以消除 CORS 依赖。若仓库尚未构建 frontend/dist，
则本模块整体跳过（纯后端 / CI 未构建场景）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_session_token

_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
pytestmark = pytest.mark.skipif(
    not (_DIST / "index.html").is_file(),
    reason="frontend/dist 未构建，跳过静态托管回归",
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_root_serves_spa(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "<div id=\"root\"" in resp.text or "id=root" in resp.text


def test_api_route_takes_precedence_over_static(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


def test_unknown_api_path_not_swallowed_as_html(client: TestClient) -> None:
    # 令牌校验通过后仍应命中路由层的 404，而不是被静态挂载吞成 index.html。
    resp = client.get(
        "/api/definitely-not-a-route",
        headers={"X-Butler-Token": get_session_token()},
    )
    assert resp.status_code == 404
