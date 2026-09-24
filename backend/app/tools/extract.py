"""内容提取工具：PDF / Word / TXT / 图片 OCR。

依赖为软依赖：缺失时返回友好提示而非崩溃，方便按需安装。
OCR 的可执行文件与语言包目录来自 ``external_tools_config``（DB 覆盖 > .env > 系统默认），
每次调用都重新读取，保证设置界面保存后无需重启即生效。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .. import external_tools_config
from ..logging_conf import get_logger
from ..security import resolve_in_sandbox

logger = get_logger(__name__)

_TEXT_EXTS = {"txt", "md", "csv", "log", "json"}
_MAX_CHARS = 20_000  # 提取文本上限，避免超长内容压爆上下文
_MAX_SOURCE_BYTES = 50 * 1024 * 1024  # 读取/解析前先限源文件，避免大文件耗尽内存
_MAX_PDF_PAGES = 500


def _truncate(text: str, max_chars: int | None = _MAX_CHARS) -> str:
    text = text.strip()
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + "\n...[已截断]"
    return text


def chunk_text(text: str, size: int) -> list[str]:
    """按字符数把文本切成不超过 size 的块，供 map-reduce 摘要使用（P2）。"""
    text = text.strip()
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


def extract_pdf(path: Path, max_chars: int | None = _MAX_CHARS) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[未安装 pypdf，无法解析 PDF]"
    reader = PdfReader(str(path))
    if len(reader.pages) > _MAX_PDF_PAGES:
        raise ValueError(f"PDF 页数超过上限 {_MAX_PDF_PAGES}: {path.name}")
    parts: list[str] = []
    length = 0
    for page in reader.pages:
        value = page.extract_text() or ""
        parts.append(value)
        length += len(value)
        if max_chars is not None and length > max_chars:
            break
    return "\n".join(parts)


def extract_docx(path: Path, max_chars: int | None = _MAX_CHARS) -> str:
    try:
        import docx
    except ImportError:
        return "[未安装 python-docx，无法解析 Word]"
    doc = docx.Document(str(path))
    parts: list[str] = []
    length = 0
    for paragraph in doc.paragraphs:
        parts.append(paragraph.text)
        length += len(paragraph.text)
        if max_chars is not None and length > max_chars:
            break
    return "\n".join(parts)


def extract_txt(path: Path, max_chars: int | None = _MAX_CHARS) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            with path.open("r", encoding=enc) as handle:
                return handle.read(None if max_chars is None else max_chars + 1)
        except (UnicodeDecodeError, OSError):
            continue
    return "[无法以常见编码读取文本文件]"


_OCR_TIMEOUT_SEC = 30  # 单张图片 OCR 超时，避免坏图/超大图无限阻塞批量任务（P2）

# pytesseract 的默认值：不在 PATH 上时由用户显式配置 tesseract_cmd 覆盖。
_DEFAULT_TESSERACT_CMD = "tesseract"
_OCR_REQUIRED_LANGUAGES: tuple[str, ...] = ("eng", "chi_sim")
_TESSDATA_DIR_ENV = "TESSDATA_PREFIX"

# 由本进程写入的 TESSDATA_PREFIX 值；用户自己设的环境变量不在此列，也不会被我们清掉。
_managed_tessdata_env: Optional[str] = None


@dataclass(frozen=True)
class TesseractSetup:
    """一次 OCR 调用实际使用的 Tesseract 配置（供调用与能力检测共用）。"""
    tesseract_cmd: str
    tessdata_dir: str
    config: str


def tessdata_args(tessdata_dir: str, *, windows: Optional[bool] = None) -> tuple[str, bool]:
    """把语言包目录转成 pytesseract 能正确传达的参数。

    返回 ``(config 片段, 是否需要 TESSDATA_PREFIX 兜底)``。

    pytesseract 内部用 ``shlex.split(config, posix=not windows)`` 切分我们给的 config
    （见 pytesseract.run_tesseract），实测（Windows + tesseract 5.0）：
    - 类 POSIX 平台会正确剥掉引号，``--tessdata-dir "带 空格"`` 可用；
    - Windows 下 posix=False **保留引号**，tesseract 会拿到带引号的路径并报
      “Error opening data file "D:\\path"/chi_sim.traineddata”，所以不能加引号；
    - Windows 下不加引号又会被路径中的空格切成多个 token，同样失败。
    因此 Windows 只在路径不含空白时走命令行参数，含空白时改用 TESSDATA_PREFIX
    环境变量（tesseract 5 期望它指向 tessdata 目录本身）；命令行参数优先级更高，
    所以两条路径不会互相干扰。
    """
    value = (tessdata_dir or "").strip()
    if not value:
        return "", False
    is_windows = os.name == "nt" if windows is None else windows
    if is_windows:
        if any(char.isspace() for char in value):
            return "", True
        return f"--tessdata-dir {value}", False
    return f'--tessdata-dir "{value}"', False


def _sync_tessdata_env(needed: bool, value: str) -> None:
    """维护 TESSDATA_PREFIX：只负责我们自己写入的那份，不动用户环境里原有的值。

    用户清空语言包配置后必须把它撤掉，否则旧值会继续影响后续 OCR 调用。
    """
    global _managed_tessdata_env
    if needed:
        os.environ[_TESSDATA_DIR_ENV] = value
        _managed_tessdata_env = value
        return
    if _managed_tessdata_env is not None:
        if os.environ.get(_TESSDATA_DIR_ENV) == _managed_tessdata_env:
            os.environ.pop(_TESSDATA_DIR_ENV, None)
        _managed_tessdata_env = None


def configure_tesseract(pytesseract_module: Any) -> TesseractSetup:
    """统一的 Tesseract 配置入口：读取当前生效配置并应用到 pytesseract。

    每次调用都重新读 external_tools_config（内部无缓存），因此设置界面保存后立即生效。
    """
    effective = external_tools_config.get_effective_config()
    command = effective.tesseract_cmd or _DEFAULT_TESSERACT_CMD
    pytesseract_module.pytesseract.tesseract_cmd = command
    config, needs_env = tessdata_args(effective.tessdata_dir)
    _sync_tessdata_env(needs_env, effective.tessdata_dir)
    return TesseractSetup(
        tesseract_cmd=command, tessdata_dir=effective.tessdata_dir, config=config
    )


def _capability(status: str, *, available: bool, setup: Optional[TesseractSetup] = None,
                version: str = "", languages: Optional[list[str]] = None,
                missing: Optional[list[str]] = None, message: str = "") -> dict[str, object]:
    """统一的能力检测返回结构；所有键恒存在，前端可安全按字段渲染。"""
    return {
        "status": status,
        "available": available,
        "version": version,
        "languages": list(languages or []),
        "missing_languages": list(missing or []),
        "tesseract_cmd": setup.tesseract_cmd if setup else "",
        "tessdata_dir": setup.tessdata_dir if setup else "",
        "message": message,
    }


def ocr_capability(
    required_languages: tuple[str, ...] = _OCR_REQUIRED_LANGUAGES,
) -> dict[str, object]:
    """探测真实二进制与语言包，而不是用配置字符串推断能力。

    任何失败都收敛成状态值，绝不抛出：OCR 是软依赖，缺它不能让后端启动或调用失败。
    """
    try:
        import pytesseract
    except ImportError:
        return _capability(
            "not_configured", available=False, message="未安装 pytesseract，无法使用 OCR"
        )

    setup = configure_tesseract(pytesseract)

    if setup.tessdata_dir and not Path(setup.tessdata_dir).is_dir():
        return _capability(
            "invalid_tessdata_dir", available=False, setup=setup,
            message=f"语言包目录不存在或不是目录：{setup.tessdata_dir}",
        )

    try:
        version = str(pytesseract.get_tesseract_version())
    except (pytesseract.TesseractNotFoundError, FileNotFoundError, OSError):
        return _capability(
            "binary_not_found", available=False, setup=setup,
            message=f"未找到 Tesseract 可执行文件（当前：{setup.tesseract_cmd}）",
        )

    try:
        languages = list(pytesseract.get_languages(config=setup.config))
    except Exception as exc:  # noqa: BLE001 - 探测失败只降级为状态值
        logger.warning("读取 Tesseract 语言包失败: %s", exc)
        return _capability(
            "error", available=False, setup=setup, version=version,
            message=f"读取语言包失败：{exc}",
        )

    missing = [lang for lang in required_languages if lang not in languages]
    if missing:
        return _capability(
            "language_pack_missing", available=True, setup=setup, version=version,
            languages=languages, missing=missing,
            message="已找到 Tesseract，但缺少语言包：" + "、".join(missing),
        )
    return _capability(
        "available", available=True, setup=setup, version=version,
        languages=languages, message="OCR 可用",
    )


def extract_image_ocr(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "[未安装 pytesseract / Pillow，无法 OCR]"

    setup = configure_tesseract(pytesseract)
    try:
        img = Image.open(str(path))
        # 中英文混合识别；未安装 chi_sim 语言包时回退到默认。加超时防止单张图卡死批量任务。
        try:
            return pytesseract.image_to_string(
                img, lang="chi_sim+eng", config=setup.config, timeout=_OCR_TIMEOUT_SEC
            )
        except pytesseract.TesseractError:
            return pytesseract.image_to_string(
                img, config=setup.config, timeout=_OCR_TIMEOUT_SEC
            )
    except RuntimeError as exc:  # pytesseract 超时抛 RuntimeError
        logger.warning("OCR 超时 %s: %s", path, exc)
        return f"[OCR 超时（超过 {_OCR_TIMEOUT_SEC}s）: {path.name}]"
    except Exception as exc:  # noqa: BLE001 - OCR 失败不应中断整体流程
        logger.warning("OCR 失败 %s: %s", path, exc)
        return f"[OCR 失败: {exc}]"


def extract_text(file_path: str, max_chars: int | None = _MAX_CHARS) -> str:
    """按扩展名分派到对应提取器，返回文本。

    ``max_chars=None`` 时不截断（供 map-reduce 摘要读取全文，见 P2）；默认截断到
    20k，避免分类等场景把超长内容压爆上下文。
    """
    p = resolve_in_sandbox(file_path, must_exist=True)
    if not p.is_file():
        raise IsADirectoryError(f"不是文件: {p}")
    size = p.stat().st_size
    if size > _MAX_SOURCE_BYTES:
        raise ValueError(
            f"文件过大（{size} 字节），超过提取上限 {_MAX_SOURCE_BYTES} 字节: {p.name}"
        )
    ext = p.suffix.lower().lstrip(".")

    if ext == "pdf":
        text = extract_pdf(p, max_chars)
    elif ext == "docx":
        text = extract_docx(p, max_chars)
    elif ext == "doc":
        text = "[不支持旧版 .doc；请先转换为 .docx]"
    elif ext in _TEXT_EXTS:
        text = extract_txt(p, max_chars)
    elif ext in {"png", "jpg", "jpeg", "bmp", "tiff", "webp"}:
        text = extract_image_ocr(p)
    else:
        text = f"[不支持的文件类型: .{ext}]"

    return _truncate(text, max_chars)
