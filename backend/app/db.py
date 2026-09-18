"""SQLite 访问层：偏好记忆、操作历史、定时任务。

使用轻量级的原生 sqlite3；每次操作获取独立连接，避免线程问题
（FastAPI + APScheduler 可能来自不同线程）。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, List, Optional

from .config import get_settings
from .logging_conf import get_logger
from .models import OperationLog, Preference, ScheduledJob

logger = get_logger(__name__)
_init_lock = threading.Lock()
_initialized = False


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    settings = get_settings()
    conn = sqlite3.connect(settings.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        with _conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS operation_log (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      TEXT NOT NULL,
                    action  TEXT NOT NULL,
                    target  TEXT NOT NULL,
                    dest    TEXT,
                    status  TEXT NOT NULL,
                    detail  TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS preferences (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    job_id      TEXT PRIMARY KEY,
                    directory   TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    cron        TEXT NOT NULL,
                    enabled     INTEGER NOT NULL DEFAULT 1
                );
                """
            )
        _initialized = True
        logger.info("SQLite 初始化完成: %s", get_settings().db_path)


# ---------- 操作日志 ----------

def log_operation(
    action: str,
    target: str,
    status: str,
    dest: Optional[str] = None,
    detail: str = "",
) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO operation_log (ts, action, target, dest, status, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), action, target, dest, status, detail),
        )
    logger.info("op=%s target=%s dest=%s status=%s", action, target, dest, status)


def recent_operations(limit: int = 100) -> List[OperationLog]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM operation_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [
        OperationLog(
            id=r["id"],
            ts=datetime.fromisoformat(r["ts"]),
            action=r["action"],
            target=r["target"],
            dest=r["dest"],
            status=r["status"],
            detail=r["detail"] or "",
        )
        for r in rows
    ]


# ---------- 偏好记忆 ----------

def set_preference(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO preferences (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_preference(key: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM preferences WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else None


def all_preferences() -> List[Preference]:
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM preferences").fetchall()
    return [Preference(key=r["key"], value=r["value"]) for r in rows]


# ---------- 定时任务 ----------

def upsert_job(job: ScheduledJob) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO scheduled_jobs (job_id, directory, instruction, cron, enabled)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(job_id) DO UPDATE SET"
            "  directory=excluded.directory, instruction=excluded.instruction,"
            "  cron=excluded.cron, enabled=excluded.enabled",
            (job.job_id, job.directory, job.instruction, job.cron, int(job.enabled)),
        )


def list_jobs() -> List[ScheduledJob]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM scheduled_jobs").fetchall()
    return [
        ScheduledJob(
            job_id=r["job_id"],
            directory=r["directory"],
            instruction=r["instruction"],
            cron=r["cron"],
            enabled=bool(r["enabled"]),
        )
        for r in rows
    ]


def delete_job(job_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM scheduled_jobs WHERE job_id=?", (job_id,))
