"""P1-4 回归：OCR 运行时配置入口（DB 覆盖 > .env > 系统默认）与能力检测。

含一段实测结论：pytesseract 在 Windows 下用 ``shlex.split(config, posix=False)`` 切分
config，引号不会被剥掉，所以 ``--tessdata-dir "含 空格"`` 会传坏；只有不含空白的裸路径
能作为单个 token 送达，含空白时改用 TESSDATA_PREFIX 环境变量。下面的用例把这些行为
锁死，避免以后“顺手加个引号”又把它改坏。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db, external_tools_config
from app.config import get_settings
from app.tools import extract

pytesseract = pytest.importorskip("pytesseract")

SPACED = r"C:\Program Files\Tesseract-OCR\tessdata"
PLAIN = r"D:\Tesseract-OCR\tessdata"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """干净库 + 清空 .env 默认值 + 重置 OCR 运行时状态（含我们写入的环境变量）。"""
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TESSERACT_CMD", "")
    monkeypatch.setenv("TESSDATA_DIR", "")
    monkeypatch.setenv("TESSDATA_PREFIX", "")  # 由 monkeypatch 负责恢复
    monkeypatch.setattr(extract, "_managed_tessdata_env", None)
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    yield tmp_path
    get_settings.cache_clear()
    db._initialized = False


# ---------- 语言包目录 → pytesseract 参数 ----------

def test_tessdata_args_empty_when_unconfigured() -> None:
    assert extract.tessdata_args("") == ("", False)
    assert extract.tessdata_args("   ") == ("", False)


def test_tessdata_args_posix_quotes_allow_spaces() -> None:
    # 类 POSIX：shlex(posix=True) 会正确剥掉引号，带空格的路径必须加引号
    config, needs_env = extract.tessdata_args(SPACED, windows=False)
    assert config == f'--tessdata-dir "{SPACED}"'
    assert needs_env is False
    assert _shlex_tokens(config, windows=False) == ["--tessdata-dir", SPACED]


def test_tessdata_args_windows_without_spaces_is_bare() -> None:
    # Windows：走 posix=False 的 shlex，裸路径是唯一能完整到达 tesseract 的形式
    config, needs_env = extract.tessdata_args(PLAIN, windows=True)
    assert config == f"--tessdata-dir {PLAIN}"
    assert needs_env is False
    assert _shlex_tokens(config, windows=True) == ["--tessdata-dir", PLAIN]


def test_tessdata_args_windows_with_spaces_falls_back_to_env() -> None:
    config, needs_env = extract.tessdata_args(SPACED, windows=True)
    assert (config, needs_env) == ("", True)
    # 反证：加引号在 Windows 下并不安全——引号会原样留在 token 里
    assert _shlex_tokens(f'--tessdata-dir "{SPACED}"', windows=True) == [
        "--tessdata-dir", f'"{SPACED}"',
    ]


def _shlex_tokens(config: str, *, windows: bool) -> list[str]:
    import shlex

    return shlex.split(config, posix=not windows)


# ---------- 统一配置入口 ----------

def test_configure_applies_effective_config(env) -> None:
    tmp_path = env
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    exe = tmp_path / "tesseract.exe"
    exe.write_text("", encoding="utf-8")
    external_tools_config.save_overrides(
        {"tesseract_cmd": str(exe), "tessdata_dir": str(tessdata)}
    )

    setup = extract.configure_tesseract(pytesseract)
    assert setup.tesseract_cmd == str(exe.resolve())
    assert pytesseract.pytesseract.tesseract_cmd == str(exe.resolve())
    assert setup.config == extract.tessdata_args(str(tessdata.resolve()))[0]


def test_configure_falls_back_to_path_lookup(env) -> None:
    """未配置时不能把 pytesseract 的默认值改坏（回退系统 PATH 查找）。"""
    setup = extract.configure_tesseract(pytesseract)
    assert setup.tesseract_cmd == "tesseract"
    assert pytesseract.pytesseract.tesseract_cmd == "tesseract"
    assert setup.config == ""


def test_new_config_takes_effect_without_clearing_settings_cache(env) -> None:
    """保存设置后立即生效：不依赖 get_settings.cache_clear()，也不依赖进程重启。"""
    tmp_path = env
    first = tmp_path / "first.exe"
    second = tmp_path / "second.exe"
    for path in (first, second):
        path.write_text("", encoding="utf-8")
    external_tools_config.save_overrides({"tesseract_cmd": str(first)})
    assert extract.configure_tesseract(pytesseract).tesseract_cmd == str(first.resolve())
    # 模拟界面第二次保存（同一进程、未清缓存）
    external_tools_config.save_overrides({"tesseract_cmd": str(second)})
    assert extract.configure_tesseract(pytesseract).tesseract_cmd == str(second.resolve())


def test_space_path_sets_tessdata_prefix_and_clears_on_reset(env) -> None:
    tmp_path = env
    spaced = tmp_path / "Program Files" / "tessdata"
    spaced.mkdir(parents=True)

    # 只有当前平台确实是 Windows + 含空格时才会用环境变量兜底
    external_tools_config.save_overrides({"tessdata_dir": str(spaced)})
    setup = extract.configure_tesseract(pytesseract)
    if os.name == "nt":
        assert setup.config == ""
        assert os.environ.get("TESSDATA_PREFIX") == str(spaced.resolve())
    else:
        assert setup.config == f'--tessdata-dir "{spaced.resolve()}"'
        assert os.environ.get("TESSDATA_PREFIX") == ""

    # 清除配置后必须撤掉我们写过的环境变量，否则旧目录会继续影响后续 OCR
    external_tools_config.save_overrides({"tessdata_dir": ""})
    extract.configure_tesseract(pytesseract)
    assert not os.environ.get("TESSDATA_PREFIX")


def test_user_tessdata_prefix_is_never_clobbered(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """用户自己设的 TESSDATA_PREFIX 不属于我们管理，清配置时不得删掉。"""
    monkeypatch.setenv("TESSDATA_PREFIX", r"E:\my-own-tessdata")
    monkeypatch.setattr(extract, "_managed_tessdata_env", None)
    extract.configure_tesseract(pytesseract)
    assert os.environ["TESSDATA_PREFIX"] == r"E:\my-own-tessdata"


# ---------- 能力检测 ----------

def _stub_tesseract(monkeypatch: pytest.MonkeyPatch, *, languages: list[str],
                    version: str = "5.3.0") -> None:
    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: version)
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": list(languages))


def test_capability_available(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_tesseract(monkeypatch, languages=["eng", "chi_sim", "osd"])
    result = extract.ocr_capability()
    assert result["status"] == "available"
    assert result["available"] is True
    assert result["version"] == "5.3.0"
    assert result["missing_languages"] == []
    assert "chi_sim" in result["languages"]


def test_capability_reports_missing_chinese_pack(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_tesseract(monkeypatch, languages=["eng"])
    result = extract.ocr_capability()
    assert result["status"] == "language_pack_missing"
    assert result["available"] is True          # 英文仍可用，只是缺中文
    assert result["missing_languages"] == ["chi_sim"]


def test_capability_reports_binary_not_found(env, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str:
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", _boom)
    result = extract.ocr_capability()
    assert result["status"] == "binary_not_found"
    assert result["available"] is False
    assert "Tesseract" in str(result["message"])


def test_capability_reports_invalid_tessdata_dir(env) -> None:
    tmp_path = env
    missing = tmp_path / "deleted-tessdata"
    missing.mkdir()
    external_tools_config.save_overrides({"tessdata_dir": str(missing)})
    missing.rmdir()  # 保存后目录被删/被移动

    result = extract.ocr_capability()
    assert result["status"] == "invalid_tessdata_dir"
    assert result["available"] is False
    assert str(missing.resolve()) in str(result["message"])


def test_capability_reports_import_error(env, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import sys

    monkeypatch.delitem(sys.modules, "pytesseract", raising=False)
    real_import = builtins.__import__

    def _fake_import(name: str, *args, **kwargs):
        if name == "pytesseract":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    result = extract.ocr_capability()
    assert result["status"] == "not_configured"
    assert result["available"] is False
    assert result["languages"] == []


def test_capability_swallows_probe_errors(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """探测本身炸了也只能是状态值：OCR 是软依赖，不能让调用方（含后端启动）失败。"""
    def _boom(config: str = "") -> list[str]:
        raise RuntimeError("tesseract crashed")

    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.3.0")
    monkeypatch.setattr(pytesseract, "get_languages", _boom)
    result = extract.ocr_capability()
    assert result["status"] == "error"
    assert result["available"] is False
    assert "crashed" in str(result["message"])


def test_capability_shape_is_stable(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """所有状态都返回同一组键，前端可以无条件按字段渲染。"""
    _stub_tesseract(monkeypatch, languages=["eng"])
    expected = {
        "status", "available", "version", "languages",
        "missing_languages", "tesseract_cmd", "tessdata_dir", "message",
    }
    assert set(extract.ocr_capability()) == expected


def test_capability_uses_configured_command(env, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path = env
    exe = tmp_path / "tesseract.exe"
    exe.write_text("", encoding="utf-8")
    external_tools_config.save_overrides({"tesseract_cmd": str(exe)})
    _stub_tesseract(monkeypatch, languages=["eng", "chi_sim"])
    result = extract.ocr_capability()
    assert result["tesseract_cmd"] == str(exe.resolve())
    assert result["status"] == "available"


# ---------- 真实二进制（本机装了 Tesseract 时才跑） ----------

@pytest.mark.skipif(
    not os.environ.get("BUTLER_TEST_TESSERACT_CMD"),
    reason="需要设置 BUTLER_TEST_TESSERACT_CMD 指向真实 tesseract 可执行文件",
)
def test_real_tesseract_end_to_end(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """真机验证整条链路：DB 配置 → configure_tesseract → 真实识别出文字。

    BUTLER_TEST_TESSDATA_DIR 可选，用来验证含空格的语言包目录也走得通。
    """
    from PIL import Image, ImageDraw

    tmp_path = env
    overrides = {"tesseract_cmd": os.environ["BUTLER_TEST_TESSERACT_CMD"]}
    tessdata = os.environ.get("BUTLER_TEST_TESSDATA_DIR")
    if tessdata:
        overrides["tessdata_dir"] = tessdata
    external_tools_config.save_overrides(overrides)

    capability = extract.ocr_capability()
    assert capability["status"] in {"available", "language_pack_missing"}, capability

    image = Image.new("RGB", (240, 70), "white")
    ImageDraw.Draw(image).text((10, 25), "hello butler", fill="black")
    image_path = tmp_path / "sample.png"
    image.save(image_path)

    text = extract.extract_image_ocr(image_path)
    assert "hello" in text.lower().replace("\n", " "), text
