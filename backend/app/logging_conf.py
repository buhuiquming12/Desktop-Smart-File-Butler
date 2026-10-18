"""日志配置：控制台 + 文件。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_dir: str) -> None:
    """配置控制台 + 文件日志。

    日志目录不可写（只读安装目录、被其他进程占用的日志文件、权限收紧）时只降级为
    控制台输出：日志是观测手段，不能因为写不了日志就让整个后端启动失败。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            Path(log_dir) / "butler.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("无法写入日志文件，已降级为仅控制台输出: %s", exc)

    # 降低第三方库噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
