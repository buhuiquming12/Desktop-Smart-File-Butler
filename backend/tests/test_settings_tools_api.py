"""P1-4 / P1-3 回归：/api/settings/tools 与 /api/settings/workspace。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, external_tools_config, sandbox_config, workspace_config
from app.config import get_settings
from app.main import app, get_session_token

_OCR_STATUSES = {
    "available", "not_configured", "binary_not_found",
    "language_pack_missing", "invalid_tessdata_dir", "error",
}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    downloads = root / "Downloads"
    for path in (root, outside, downloads):
        path.mkdir(parents=True)
    monkeypatch.setenv("SANDBOX_ROOTS", str(root))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TESSERACT_CMD", "")
    monkeypatch.setenv("TESSDATA_DIR", "")
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    client = TestClient(app)
    client.headers.update({"X-Butler-Token": get_session_token()})
    yield SimpleEnv(tmp_path, root, outside, downloads, client)
    get_settings.cache_clear()
    db._initialized = False


class SimpleEnv:
    def __init__(self, tmp_path: Path, root: Path, outside: Path,
                 downloads: Path, client: TestClient) -> None:
        self.tmp_path = tmp_path
        self.root = root
        self.outside = outside
        self.downloads = downloads
        self.client = client


# ---------- OCR / 外部工具 ----------

def test_get_tools_returns_effective_config_and_capability(env: SimpleEnv) -> None:
    response = env.client.get("/api/settings/tools")
    assert response.status_code == 200
    body = response.json()
    assert body["tesseract_cmd"] == ""
    assert body["tessdata_dir"] == ""
    assert body["sources"] == {"tesseract_cmd": "env", "tessdata_dir": "env"}
    assert body["ocr"]["status"] in _OCR_STATUSES
    assert isinstance(body["ocr"]["languages"], list)


def test_put_tools_saves_and_reports_immediately(env: SimpleEnv) -> None:
    exe = env.tmp_path / "tesseract.exe"
    exe.write_text("", encoding="utf-8")
    tessdata = env.tmp_path / "tessdata"
    tessdata.mkdir()

    response = env.client.put("/api/settings/tools", json={
        "tesseract_cmd": str(exe),
        "tessdata_dir": str(tessdata),
    })
    assert response.status_code == 200
    body = response.json()
    assert body["tesseract_cmd"] == str(exe.resolve())
    assert body["tessdata_dir"] == str(tessdata.resolve())
    assert body["sources"] == {"tesseract_cmd": "database", "tessdata_dir": "database"}
    # 保存后立刻用新配置重新检测（未重启、未清 get_settings 缓存）
    assert body["ocr"]["tesseract_cmd"] == str(exe.resolve())

    # 独立 GET 也必须一致（无缓存）
    assert env.client.get("/api/settings/tools").json()["tesseract_cmd"] == str(exe.resolve())


def test_put_tools_rejects_missing_executable(env: SimpleEnv) -> None:
    response = env.client.put("/api/settings/tools", json={
        "tesseract_cmd": str(env.tmp_path / "nope" / "tesseract.exe"),
    })
    assert response.status_code == 422
    assert "Tesseract 可执行文件不存在" in response.json()["detail"]
    assert db.get_tool_config() == {}


def test_put_tools_rejects_missing_tessdata_dir(env: SimpleEnv) -> None:
    response = env.client.put("/api/settings/tools", json={
        "tessdata_dir": str(env.tmp_path / "no-such-dir"),
    })
    assert response.status_code == 422
    assert "语言包目录不存在" in response.json()["detail"]
    assert db.get_tool_config() == {}


def test_put_tools_rejects_file_as_directory(env: SimpleEnv) -> None:
    exe = env.tmp_path / "tesseract.exe"
    exe.write_text("", encoding="utf-8")
    response = env.client.put("/api/settings/tools", json={"tessdata_dir": str(exe)})
    assert response.status_code == 422
    assert db.get_tool_config() == {}


def test_put_tools_empty_string_clears_override(env: SimpleEnv) -> None:
    exe = env.tmp_path / "tesseract.exe"
    exe.write_text("", encoding="utf-8")
    env.client.put("/api/settings/tools", json={"tesseract_cmd": str(exe)})
    assert external_tools_config.get_effective_config().tesseract_cmd == str(exe.resolve())

    response = env.client.put("/api/settings/tools", json={"tesseract_cmd": ""})
    assert response.status_code == 200
    assert response.json()["tesseract_cmd"] == ""
    assert response.json()["sources"]["tesseract_cmd"] == "env"
    assert "tesseract_cmd" not in db.get_tool_config()


def test_put_tools_leaves_unsent_field_untouched(env: SimpleEnv) -> None:
    tessdata = env.tmp_path / "tessdata"
    tessdata.mkdir()
    env.client.put("/api/settings/tools", json={"tessdata_dir": str(tessdata)})
    env.client.put("/api/settings/tools", json={"tesseract_cmd": ""})  # 只提交一个字段
    assert external_tools_config.get_effective_config().tessdata_dir == str(tessdata.resolve())


def test_put_tools_rejects_unknown_field(env: SimpleEnv) -> None:
    exe = env.tmp_path / "tesseract.exe"
    exe.write_text("", encoding="utf-8")
    response = env.client.put("/api/settings/tools", json={"tesseract_cmd": str(exe)})
    assert response.status_code == 200
    # 未知键不进库（配置面保持收敛）
    assert set(db.get_tool_config()) <= {"tesseract_cmd", "tessdata_dir"}


# ---------- 默认管理目录 ----------

def test_workspace_starts_unconfigured(env: SimpleEnv) -> None:
    response = env.client.get("/api/settings/workspace")
    assert response.status_code == 200
    body = response.json()
    assert body["default_managed_root"] == ""
    assert body["valid"] is False
    assert body["allowed_roots"] == [str(env.root.resolve())]


def test_put_workspace_inside_authorized_root(env: SimpleEnv) -> None:
    response = env.client.put(
        "/api/settings/workspace", json={"default_managed_root": str(env.downloads)}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["default_managed_root"] == str(env.downloads.resolve())
    assert body["valid"] is True
    assert body["message"] == ""
    assert env.client.get("/api/settings/workspace").json()["valid"] is True


def test_put_workspace_outside_authorized_root_rejected(env: SimpleEnv) -> None:
    response = env.client.put(
        "/api/settings/workspace", json={"default_managed_root": str(env.outside)}
    )
    assert response.status_code == 422
    assert "授权目录" in response.json()["detail"]
    assert workspace_config.effective_default_root() is None


def test_put_workspace_empty_clears(env: SimpleEnv) -> None:
    env.client.put("/api/settings/workspace", json={"default_managed_root": str(env.downloads)})
    response = env.client.put("/api/settings/workspace", json={"default_managed_root": ""})
    assert response.status_code == 200
    assert response.json()["default_managed_root"] == ""
    assert response.json()["valid"] is False


def test_workspace_reports_stale_value_after_sandbox_shrink(env: SimpleEnv) -> None:
    """授权目录收紧后，默认管理目录必须失效并如实告知（而不是继续生效）。"""
    env.client.put("/api/settings/workspace", json={"default_managed_root": str(env.downloads)})
    shrink = env.root / "Pictures"
    shrink.mkdir()
    assert env.client.put("/api/settings/sandbox", json={"roots": [str(shrink)]}).status_code == 200

    body = env.client.get("/api/settings/workspace").json()
    assert body["default_managed_root"] == ""
    assert body["valid"] is False
    assert body["allowed_roots"] == [str(shrink.resolve())]
    # 越界值已被就地清除，界面显示的是“未配置”，不会留下一个点了就错的路径
    assert body["stored_default_managed_root"] == ""
    assert workspace_config.stored_default_root() is None


def test_workspace_explains_deleted_directory(env: SimpleEnv) -> None:
    """目录被删除（但在库里的值仍属授权范围）时，界面要能解释为什么失效。"""
    env.client.put("/api/settings/workspace", json={"default_managed_root": str(env.downloads)})
    env.downloads.rmdir()

    body = env.client.get("/api/settings/workspace").json()
    assert body["valid"] is False
    assert body["default_managed_root"] == ""
    assert body["stored_default_managed_root"] == str(env.downloads.resolve())
    assert "重新选择" in body["message"]


def test_workspace_endpoints_require_session_token(env: SimpleEnv) -> None:
    """新增接口沿用同一套令牌 + 来源校验，不额外开口子。"""
    for path in ("/api/settings/tools", "/api/settings/workspace"):
        response = env.client.get(path, headers={"X-Butler-Token": ""})
        assert response.status_code == 401

    sandbox_config.save_roots([str(env.root)])  # 保证有授权目录，排除无关原因
    assert env.client.get("/api/settings/tools").status_code == 200
