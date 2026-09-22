"""LangGraph checkpoint 的创建、时间解析与保留策略。"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from langgraph.checkpoint.sqlite import SqliteSaver

from ..config import get_settings
from ..logging_conf import get_logger

logger = get_logger(__name__)


def build_checkpointer() -> SqliteSaver:
    """创建允许 API 与调度线程共享的 SQLite checkpointer。"""
    path = Path(get_settings().db_path).parent / "checkpoints.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


_UUID_EPOCH = datetime(1582, 10, 15, tzinfo=timezone.utc)


def checkpoint_created_at(checkpoint_id: Any) -> Optional[datetime]:
    """从 LangGraph 使用的 UUIDv6 checkpoint id 解析生成时刻。"""
    try:
        value = uuid.UUID(str(checkpoint_id))
    except (ValueError, AttributeError, TypeError):
        return None
    if value.version != 6:
        return None
    high48 = value.int >> 80
    intervals = (
        ((high48 >> 16) << 28)
        | ((high48 & 0xFFFF) << 12)
        | ((value.int >> 64) & 0x0FFF)
    )
    return _UUID_EPOCH + timedelta(microseconds=intervals / 10)


def cleanup_checkpoints(
    checkpointer: SqliteSaver,
    state_reader: Any,
    *,
    max_sessions: int = 100,
    max_age_days: int = 30,
) -> int:
    """删除超过保留期的旧会话；待审批和最近会话始终保留。"""
    conn = getattr(checkpointer, "conn", None) or getattr(checkpointer, "connection", None)
    if conn is None:
        return 0
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    except sqlite3.DatabaseError:
        return 0
    if "checkpoints" not in tables:
        return 0
    delete_sql = [
        f"DELETE FROM {name} WHERE thread_id=?"
        for name in ("writes", "checkpoints")
        if name in tables
    ]
    try:
        rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    except sqlite3.DatabaseError:
        return 0

    sessions = []
    for row in rows:
        thread_id = row[0]
        try:
            state = state_reader(thread_id) or {}
        except Exception:  # noqa: BLE001
            state = {}
        if state.get("status") == "waiting_approval" or state.get("pending_approval"):
            continue
        try:
            latest = conn.execute(
                "SELECT MAX(checkpoint_id) FROM checkpoints WHERE thread_id=?",
                (thread_id,),
            ).fetchone()[0]
        except sqlite3.DatabaseError:
            latest = ""
        sessions.append((str(latest), thread_id))
    sessions.sort(reverse=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    victims = []
    for index, (latest, thread_id) in enumerate(sessions):
        if index < max_sessions:
            continue
        created = checkpoint_created_at(latest)
        if created is None or created >= cutoff:
            continue
        victims.append(thread_id)

    removed = 0
    for thread_id in victims:
        try:
            for sql in delete_sql:
                conn.execute(sql, (thread_id,))
            conn.commit()
            removed += 1
        except sqlite3.DatabaseError:
            logger.exception("checkpoint 清理失败 thread=%s，已回滚", thread_id)
            conn.rollback()
    if removed:
        logger.info("checkpoint 清理：删除 %d 个超过 %d 天的旧会话", removed, max_age_days)
    return removed
