"""有效沙箱根目录：在 .env 默认值之上叠加前端保存到 SQLite 的覆盖项。

照抄 ``llm_config`` 的“DB 覆盖 .env”模式（P1-3）。桌面应用允许用户在界面里选择
可操作的文件夹，而不必手改 backend/.env。

覆盖项存于 ``preferences`` 表的保留键 ``__sandbox_roots__``（以 ; 分隔的绝对路径），
该键对普通偏好接口不可见、也不会喂给规划器。

**故意不加缓存**：``effective_roots`` 每次读库，保证保存后立即生效，并避免与测试里
``get_settings.cache_clear()`` 造成的陈旧缓存耦合（对应“改完必须立即生效”的要求）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from . import db
from .config import get_settings
from .logging_conf import get_logger

logger = get_logger(__name__)

ROOTS_KEY = "__sandbox_roots__"


def _parse(raw: str) -> List[Path]:
    try:
        decoded = json.loads(raw)
        values = decoded if isinstance(decoded, list) and all(isinstance(v, str) for v in decoded) else []
    except (json.JSONDecodeError, TypeError):
        values = raw.split(";")  # legacy format
    roots: List[Path] = []
    for part in values:
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return roots


def effective_roots() -> List[Path]:
    """DB 覆盖存在则用之，否则回退 .env 的 SANDBOX_ROOTS。"""
    override = db.get_preference(ROOTS_KEY)
    if override:
        roots = _parse(override)
        if roots:
            return roots
    return get_settings().sandbox_root_paths


class InvalidSandboxRoot(ValueError):
    """提交的沙箱根目录不是一个存在的目录。"""


def save_roots(paths: List[str]) -> List[str]:
    """校验并保存沙箱根目录覆盖项；空列表清除覆盖、回退 .env。返回规范化后的路径。

    每个路径必须是已存在的目录，否则抛 ``InvalidSandboxRoot``。
    """
    normalized: List[str] = []
    for raw in paths:
        raw = (raw or "").strip()
        if not raw:
            continue
        resolved = Path(raw).expanduser().resolve()
        if not resolved.is_dir():
            raise InvalidSandboxRoot(f"不是有效目录: {resolved}")
        normalized.append(str(resolved))

    # 去重并保持顺序
    seen: set[str] = set()
    unique = [p for p in normalized if not (p in seen or seen.add(p))]
    db.set_preference(
        ROOTS_KEY, json.dumps(unique, ensure_ascii=False), allow_reserved=True
    )  # 空串表示清除，回退 .env
    # 授权目录收紧后，默认管理目录可能已经越界：就地清除，避免界面显示一个已失效的值
    # （读取侧 effective_default_root 也会再拦一道，见 workspace_config）。
    from .workspace_config import prune_out_of_bounds  # 局部导入避免循环引用

    if prune_out_of_bounds():
        logger.info("授权目录变更：默认管理目录越界，已清除")
    return unique
