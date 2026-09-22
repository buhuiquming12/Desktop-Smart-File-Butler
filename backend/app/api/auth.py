"""本地桌面会话令牌、来源校验与 session 文件。"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from ..config import get_settings
from ..logging_conf import get_logger

logger = get_logger(__name__)

_SESSION_TOKEN = secrets.token_urlsafe(32)
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def get_session_token() -> str:
    return _SESSION_TOKEN


def session_file_path() -> Path:
    override = os.environ.get("BUTLER_SESSION_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".desktop-smart-file-butler" / "session.json"


def write_session_file() -> None:
    """把令牌与监听端口写给 Electron 主进程；令牌本身不进日志。"""
    settings = get_settings()
    path = session_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"token": _SESSION_TOKEN, "host": settings.host, "port": settings.port}
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info("会话令牌已写入 %s（令牌本身不记录到日志）", path)
    except OSError:
        logger.exception("写入会话令牌文件失败: %s", path)


def origin_allowed(origin: Optional[str]) -> bool:
    """仅允许本机 HTTP(S) Origin；无 Origin 的非浏览器请求仍需令牌。"""
    if not origin:
        return True
    if origin == "null":
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.hostname in _LOCAL_HOSTS


def token_valid(token: Optional[str]) -> bool:
    return bool(token) and secrets.compare_digest(token, _SESSION_TOKEN)
