"""SQLite 访问层：偏好记忆、操作历史、定时任务。

使用轻量级的原生 sqlite3；每次操作获取独立连接，避免线程问题
（FastAPI + APScheduler 可能来自不同线程）。
"""
from __future__ import annotations

import contextvars
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

# 当前正在执行的 Agent 会话线程 id；由运行时在节点执行前设置，
# log_operation 自动带上，用于按 thread_id 回滚（见 P1-1）。
_current_thread_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_thread_id", default=None
)


@contextmanager
def operation_thread(thread_id: Optional[str]) -> Iterator[None]:
    """在该上下文内记录的操作都归属到给定 thread_id。"""
    token = _current_thread_id.set(thread_id)
    try:
        yield
    finally:
        _current_thread_id.reset(token)


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
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        TEXT NOT NULL,
                    action    TEXT NOT NULL,
                    target    TEXT NOT NULL,
                    dest      TEXT,
                    status    TEXT NOT NULL,
                    detail    TEXT DEFAULT '',
                    thread_id TEXT
                );

                CREATE TABLE IF NOT EXISTS preferences (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS llm_config (
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
            # 迁移：为老用户已存在的 operation_log 补 thread_id 列（CREATE IF NOT EXISTS
            # 不会给旧表加列）。新增可空列对既有行安全，值为 NULL（见 P1-1）。
            columns = {row["name"] for row in c.execute("PRAGMA table_info(operation_log)")}
            if "thread_id" not in columns:
                c.execute("ALTER TABLE operation_log ADD COLUMN thread_id TEXT")
                logger.info("operation_log 迁移：已新增 thread_id 列")
        _initialized = True
        logger.info("SQLite 初始化完成: %s", get_settings().db_path)


# ---------- 操作日志 ----------

def log_operation(
    action: str,
    target: str,
    status: str,
    dest: Optional[str] = None,
    detail: str = "",
    thread_id: Optional[str] = None,
) -> None:
    tid = thread_id if thread_id is not None else _current_thread_id.get()
    with _conn() as c:
        c.execute(
            "INSERT INTO operation_log (ts, action, target, dest, status, detail, thread_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                action,
                target,
                dest,
                status,
                detail,
                tid,
            ),
        )
    logger.info("op=%s target=%s dest=%s status=%s thread=%s", action, target, dest, status, tid)


def _row_to_operation(r: sqlite3.Row) -> OperationLog:
    keys = r.keys()
    return OperationLog(
        id=r["id"],
        ts=datetime.fromisoformat(r["ts"]),
        action=r["action"],
        target=r["target"],
        dest=r["dest"],
        status=r["status"],
        detail=r["detail"] or "",
        thread_id=r["thread_id"] if "thread_id" in keys else None,
    )


def recent_operations(limit: int = 100) -> List[OperationLog]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM operation_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_operation(r) for r in rows]


def get_operation(op_id: int) -> Optional[OperationLog]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM operation_log WHERE id=?", (op_id,)
        ).fetchone()
    return _row_to_operation(row) if row else None


def operations_for_thread(thread_id: str) -> List[OperationLog]:
    """返回某会话的操作，按 id 升序（便于回滚时倒序处理）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM operation_log WHERE thread_id=? ORDER BY id ASC",
            (thread_id,),
        ).fetchall()
    return [_row_to_operation(r) for r in rows]


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


# ---------- 模型配置（前端可写，覆盖 .env 默认值） ----------

def get_llm_config() -> dict[str, str]:
    """返回所有已保存的模型配置覆盖项（键值对）。"""
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM llm_config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_llm_config(values: dict[str, str]) -> None:
    """批量写入 / 更新模型配置覆盖项。值为空字符串表示清除该覆盖。"""
    with _conn() as c:
        for key, value in values.items():
            if value == "":
                c.execute("DELETE FROM llm_config WHERE key=?", (key,))
            else:
                c.execute(
                    "INSERT INTO llm_config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )


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
