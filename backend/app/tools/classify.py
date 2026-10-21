"""P3：按内容分类（带缓存、预算与明确回退）。

约定：
- 只有“真的读到了正文并且模型给出类别”才标记 ``basis="按内容"``；
- 其余一切情况都回退到扩展名规则，并在 ``fallback_reason`` 里写清原因，
  绝不编造置信度，也不把回退结果说成内容判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .. import db
from ..logging_conf import get_logger
from . import categories, extract

logger = get_logger(__name__)

# 可按内容判断的格式（与 extract_text 支持范围一致）。
CONTENT_EXTS = {
    "pdf", "docx", "txt", "md", "csv", "log", "json",
    "png", "jpg", "jpeg", "bmp", "tiff", "webp",
}

MODES = ("extension", "content")


@dataclass
class Budget:
    """一次分类任务可用的模型调用次数；用尽后统一回退。"""

    remaining: int
    used: int = 0

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        self.used += 1
        return True


def basis_for_extension(ext: str) -> str:
    return f"按类型：.{ext or '(无扩展名)'} → {categories.rule_category(ext)}"


def classify_path(
    path: Path,
    *,
    llm: Any = None,
    mode: str = "content",
    budget: Optional[Budget] = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """返回 ``{category, basis, fallback, fallback_reason}``。"""
    if mode not in MODES:
        raise ValueError(f"未知分类模式: {mode}")
    ext = path.suffix.lower().lstrip(".")
    rule_category = categories.rule_category(ext)
    if mode == "extension":
        return {
            "category": rule_category,
            "basis": basis_for_extension(ext),
            "fallback": False,
            "fallback_reason": "",
        }

    try:
        stat = path.stat()
        size, mtime_ns = int(stat.st_size), int(stat.st_mtime_ns)
    except OSError as exc:
        return {
            "category": rule_category,
            "basis": f"按类型（无法读取文件：{exc}）",
            "fallback": True,
            "fallback_reason": "文件不可读",
        }

    if use_cache:
        cached = db.get_classification(str(path), size, mtime_ns)
        if cached and cached.get("mode") == "content":
            return {
                "category": cached["category"],
                "basis": f"按内容（缓存）：{cached['basis']}",
                "fallback": False,
                "fallback_reason": "",
            }

    if ext not in CONTENT_EXTS:
        return {
            "category": rule_category,
            "basis": "按类型（该格式不支持内容提取）",
            "fallback": True,
            "fallback_reason": "格式不支持内容提取",
        }
    if llm is None:
        return {
            "category": rule_category,
            "basis": "按类型（模型未配置，无法按内容判断）",
            "fallback": True,
            "fallback_reason": "模型未配置",
        }
    if budget is not None and not budget.take():
        return {
            "category": rule_category,
            "basis": "按类型（内容分类达到本次预算上限）",
            "fallback": True,
            "fallback_reason": "达到分类预算上限",
        }
    try:
        text = extract.extract_text(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.info("内容分类提取失败 %s: %s", path, exc)
        return {
            "category": rule_category,
            "basis": f"按类型（内容提取失败：{exc}）",
            "fallback": True,
            "fallback_reason": "内容提取失败",
        }
    if not text.strip() or text.startswith("["):
        return {
            "category": rule_category,
            "basis": "按类型（没有可用的正文）",
            "fallback": True,
            "fallback_reason": "没有可用正文",
        }

    from ..agent.tool_executor import ToolExecutor  # 延迟导入，避免循环依赖

    category = ToolExecutor(llm).llm_classify(path.name, rule_category, text)
    if not category:
        return {
            "category": rule_category,
            "basis": "按类型（模型未给出类别）",
            "fallback": True,
            "fallback_reason": "模型未给出类别",
        }
    if use_cache:
        db.set_classification(str(path), size, mtime_ns, "content", category, f"模型判定：{path.name}")
    return {
        "category": category,
        "basis": "按内容：模型依据文件名与正文判定",
        "fallback": False,
        "fallback_reason": "",
    }
