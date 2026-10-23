"""日志配置：控制台 + 文件，并在出口统一脱敏本地密钥。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


class SecretRedactionFilter(logging.Filter):
    """在日志出口替换已知密钥与常见 API Key 形态。

    过滤发生在 handler 层（而不是各个调用点），因此即使某处误把密钥放进
    ``logger.info`` / 异常堆栈，也不会落进控制台或日志文件。密钥读取失败时
    脱敏模块本身不能抛错，否则会吞掉原始日志，所以这里整体兜底。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from .security.secrets import redact_text

            message = record.getMessage()
            redacted = redact_text(message)
            if redacted != message:
                record.msg = redacted
                record.args = ()
            if record.exc_info and not record.exc_text:
                record.exc_text = redact_text(
                    logging.Formatter().formatException(record.exc_info)
                )
        except Exception:  # noqa: BLE001 - 脱敏失败也不能阻断日志
            pass
        return True


def _attach(handler: logging.Handler) -> None:
    handler.addFilter(SecretRedactionFilter())


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
    _attach(console)
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
        _attach(file_handler)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("无法写入日志文件，已降级为仅控制台输出: %s", exc)

    # 降低第三方库噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
