"""P1-4 回归：外部工具（OCR）路径的覆盖优先级 DB > .env > 系统默认。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import db, external_tools_config
from app.config import get_settings


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """干净库 + .env 默认值：TESSERACT_CMD / TESSDATA_DIR 都指向 tmp 下的真实路径。"""
    env_exe = tmp_path / "env-tesseract.exe"
    env_exe.write_text("", encoding="utf-8")
    env_data = tmp_path / "env-tessdata"
    env_data.mkdir()
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TESSERACT_CMD", str(env_exe))
    monkeypatch.setenv("TESSDATA_DIR", str(env_data))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield tmp_path, env_exe, env_data
    get_settings.cache_clear()
    db._initialized = False


def test_env_used_without_override(env) -> None:
    _, env_exe, env_data = env
    config = external_tools_config.get_effective_config()
    assert config.tesseract_cmd == str(env_exe)
    assert config.tessdata_dir == str(env_data)


def test_db_override_beats_env(env) -> None:
    tmp_path, env_exe, env_data = env
    db_exe = tmp_path / "db-tesseract.exe"
    db_exe.write_text("", encoding="utf-8")
    external_tools_config.save_overrides({"tesseract_cmd": str(db_exe)})

    config = external_tools_config.get_effective_config()
    assert config.tesseract_cmd == str(db_exe)      # 覆盖生效
    assert config.tessdata_dir == str(env_data)     # 未覆盖的键仍用 .env
    assert env_exe.exists()                          # 原 .env 路径不受影响


def test_empty_override_falls_back_to_env(env) -> None:
    _, env_exe, _ = env
    external_tools_config.save_overrides({"tesseract_cmd": "/tmp/whatever"})
    external_tools_config.save_overrides({"tesseract_cmd": ""})  # 空串清除覆盖
    assert external_tools_config.get_effective_config().tesseract_cmd == str(env_exe)
    assert "tesseract_cmd" not in db.get_tool_config()


def test_unknown_keys_ignored(env) -> None:
    external_tools_config.save_overrides({"ffmpeg_cmd": "/usr/bin/ffmpeg"})
    assert db.get_tool_config() == {}


def test_sources_report_effective_origin(env) -> None:
    tmp_path, _, _ = env
    assert external_tools_config.sources() == {
        "tesseract_cmd": "env",
        "tessdata_dir": "env",
    }
    db_exe = tmp_path / "db.exe"
    db_exe.write_text("", encoding="utf-8")
    external_tools_config.save_overrides({"tesseract_cmd": str(db_exe)})
    assert external_tools_config.sources() == {
        "tesseract_cmd": "database",
        "tessdata_dir": "env",
    }


def test_reject_missing_executable(env) -> None:
    tmp_path, _, _ = env
    with pytest.raises(external_tools_config.InvalidToolConfig) as excinfo:
        external_tools_config.validate_overrides(
            {"tesseract_cmd": str(tmp_path / "nope" / "tesseract.exe")}
        )
    assert "不存在" in str(excinfo.value)
    assert db.get_tool_config() == {}


def test_reject_directory_as_executable(env) -> None:
    tmp_path, _, _ = env
    with pytest.raises(external_tools_config.InvalidToolConfig):
        external_tools_config.validate_overrides({"tesseract_cmd": str(tmp_path)})


def test_reject_file_as_tessdata_dir(env) -> None:
    _, env_exe, _ = env
    with pytest.raises(external_tools_config.InvalidToolConfig) as excinfo:
        external_tools_config.validate_overrides({"tessdata_dir": str(env_exe)})
    assert "语言包目录" in str(excinfo.value)


def test_reject_missing_tessdata_dir(env) -> None:
    tmp_path, _, _ = env
    with pytest.raises(external_tools_config.InvalidToolConfig):
        external_tools_config.validate_overrides(
            {"tessdata_dir": str(tmp_path / "not-there")}
        )


def test_reject_empty_string_is_clear_not_error(env) -> None:
    # 空串是“清除覆盖”，不是非法值
    assert external_tools_config.validate_overrides(
        {"tesseract_cmd": "", "tessdata_dir": "  "}
    ) == {"tesseract_cmd": "", "tessdata_dir": ""}


def test_path_with_spaces_round_trips(env) -> None:
    """含空格的路径（Windows 上很常见）必须能原样保存与读回。"""
    tmp_path, _, _ = env
    spaced_dir = tmp_path / "Program Files" / "tessdata"
    spaced_dir.mkdir(parents=True)
    spaced_exe = tmp_path / "Program Files" / "tesseract.exe"
    spaced_exe.write_text("", encoding="utf-8")

    external_tools_config.save_overrides(
        {"tesseract_cmd": f"  {spaced_exe}  ", "tessdata_dir": str(spaced_dir)}
    )
    config = external_tools_config.get_effective_config()
    assert config.tesseract_cmd == str(spaced_exe.resolve())
    assert config.tessdata_dir == str(spaced_dir.resolve())


def test_legacy_database_gets_tool_config_table(tmp_path: Path, monkeypatch) -> None:
    """老用户的库没有 tool_config 表：升级后必须能用，且原有数据不丢。"""
    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_db)
    conn.executescript(
        """
        CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE llm_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO preferences (key, value) VALUES ('default_directory', 'D:\\Downloads');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(legacy_db))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    try:
        db.init_db()
        assert db.get_tool_config() == {}
        assert db.get_preference("default_directory") == "D:\\Downloads"
        db.set_tool_config({"tesseract_cmd": "D:\\Tesseract-OCR\\tesseract.exe"})
        assert db.get_tool_config() == {
            "tesseract_cmd": "D:\\Tesseract-OCR\\tesseract.exe"
        }
    finally:
        get_settings.cache_clear()
        db._initialized = False


def test_missing_table_degrades_to_empty(tmp_path: Path, monkeypatch) -> None:
    """表还没建（只读探针早于 init_db 调用 OCR）时返回空，而不是抛异常。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    try:
        assert db.get_tool_config() == {}
    finally:
        get_settings.cache_clear()
        db._initialized = False
