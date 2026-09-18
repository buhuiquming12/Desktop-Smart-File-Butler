"""内容提取工具：PDF / Word / TXT / 图片 OCR。

依赖为软依赖：缺失时返回友好提示而非崩溃，方便按需安装。
"""
from __future__ import annotations

from pathlib import Path

from ..config import get_settings
from ..logging_conf import get_logger
from ..security import resolve_in_sandbox

logger = get_logger(__name__)

_TEXT_EXTS = {"txt", "md", "csv", "log", "json"}
_MAX_CHARS = 20_000  # 提取文本上限，避免超长内容压爆上下文


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) > _MAX_CHARS:
        return text[:_MAX_CHARS] + f"\n...[已截断，原文 {len(text)} 字符]"
    return text


def extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "[未安装 pypdf，无法解析 PDF]"
    reader = PdfReader(str(path))
    parts = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(parts)


def extract_docx(path: Path) -> str:
    try:
        import docx
    except ImportError:
        return "[未安装 python-docx，无法解析 Word]"
    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs)


def extract_txt(path: Path) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, OSError):
            continue
    return "[无法以常见编码读取文本文件]"


def extract_image_ocr(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "[未安装 pytesseract / Pillow，无法 OCR]"

    cmd = get_settings().tesseract_cmd
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        img = Image.open(str(path))
        # 中英文混合识别；未安装 chi_sim 语言包时回退到默认
        try:
            return pytesseract.image_to_string(img, lang="chi_sim+eng")
        except pytesseract.TesseractError:
            return pytesseract.image_to_string(img)
    except Exception as exc:  # noqa: BLE001 - OCR 失败不应中断整体流程
        logger.warning("OCR 失败 %s: %s", path, exc)
        return f"[OCR 失败: {exc}]"


def extract_text(file_path: str) -> str:
    """按扩展名分派到对应提取器，返回截断后的文本。"""
    p = resolve_in_sandbox(file_path, must_exist=True)
    ext = p.suffix.lower().lstrip(".")

    if ext == "pdf":
        text = extract_pdf(p)
    elif ext in {"docx", "doc"}:
        text = extract_docx(p)
    elif ext in _TEXT_EXTS:
        text = extract_txt(p)
    elif ext in {"png", "jpg", "jpeg", "bmp", "tiff", "webp"}:
        text = extract_image_ocr(p)
    else:
        text = f"[不支持的文件类型: .{ext}]"

    return _truncate(text)
