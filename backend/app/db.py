"""SQLite 访问层：偏好记忆、操作历史、定时任务。

使用轻量级的原生 sqlite3；每次操作获取独立连接，避免线程问题
（FastAPI + APScheduler 可能来自不同线程）。
"""
from __future__ import annotations

import contextvars
import json
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
    # busy_timeout 必须排在任何可能取锁的语句之前：切换 journal_mode 需要写锁，
    # 先设好等待时长才能等到锁，而不是立刻抛 "database is locked"（B9）。
    conn.execute("PRAGMA busy_timeout=10000")
    # synchronous 是连接级设置，每条连接都要设（P2）。
    conn.execute("PRAGMA synchronous=NORMAL")
    # journal_mode=WAL 是写进库头的持久属性，只在建库时设置一次即可（见 init_db）。
    # 此前每条连接都重复设置，等于每次连接都去抢一次写锁，是多余的争用来源。
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
            # WAL 让 APScheduler 线程与 API 线程的读写并发不再互相阻塞成
            # "database is locked"（P2）。该属性持久化在库头，建库时设一次即可。
            c.execute("PRAGMA journal_mode=WAL")
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

                CREATE TABLE IF NOT EXISTS tool_config (
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

                CREATE TABLE IF NOT EXISTS scan_manifests (
                    scan_id    TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    directory  TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
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


# ---------- 扫描清单 ----------

def save_manifest(scan_id: str, directory: str, items: list[dict], metadata: dict) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO scan_manifests(scan_id, created_at, directory, items_json, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (scan_id, datetime.now(timezone.utc).isoformat(), directory,
             json.dumps(items, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False)),
        )


def get_manifest(scan_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM scan_manifests WHERE scan_id=?", (scan_id,)).fetchone()
    if not row:
        return None
    return {
        "scan_id": row["scan_id"],
        "directory": row["directory"],
        "items": json.loads(row["items_json"]),
        "metadata": json.loads(row["metadata_json"]),
    }


# ---------- 偏好记忆 ----------

def validate_public_preference_key(key: str) -> str:
    """校验来自 REST / Agent 的普通偏好键。

    ``__`` 前缀由后端内部配置占用。校验必须位于共享边界，不能只放在 API
    路由中，否则 Agent 工具可以绕过路由直接改写沙箱等安全配置。
    """
    normalized = key.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("偏好键不能为空且最多 200 字符")
    if normalized.startswith("__"):
        raise ValueError("保留键不可通过普通偏好接口修改")
    if any(
        secret in normalized.lower()
        for secret in ("api_key", "token", "secret", "password")
    ):
        raise ValueError("密钥、令牌和密码不能保存为偏好")
    return normalized


def set_preference(
    key: str, value: str, *, allow_reserved: bool = False
) -> None:
    """保存偏好；保留键仅允许受控的后端配置代码显式写入。"""
    normalized = key.strip()
    if not allow_reserved:
        normalized = validate_public_preference_key(normalized)
    with _conn() as c:
        c.execute(
            "INSERT INTO preferences (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (normalized, value),
        )


def get_preference(key: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM preferences WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else None


def all_preferences() -> List[Preference]:
    """返回用户偏好。以 __ 开头的保留键（如内部沙箱配置）不对外暴露、也不喂给规划器。"""
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM preferences").fetchall()
    return [
        Preference(key=r["key"], value=r["value"])
        for r in rows
        if not r["key"].startswith("__")
    ]


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


# ---------- 外部工具配置（前端可写，覆盖 .env 默认值） ----------

def get_tool_config() -> dict[str, str]:
    """返回所有已保存的外部工具配置覆盖项（键值对）。

    表尚未建立时（例如只读探针在 init_db 之前调用 OCR 能力检测）返回空字典并记日志：
    外部工具是软依赖，读不到配置只应降级为“用默认值”，不能反过来拖垮调用方。
    """
    try:
        with _conn() as c:
            rows = c.execute("SELECT key, value FROM tool_config").fetchall()
    except sqlite3.OperationalError:
        logger.warning("tool_config 表不可用，按未配置处理（外部工具将回退 .env / 系统默认）")
        return {}
    return {r["key"]: r["value"] for r in rows}


def set_tool_config(values: dict[str, str]) -> None:
    """批量写入 / 更新外部工具配置覆盖项。值为空字符串表示清除该覆盖。"""
    with _conn() as c:
        for key, value in values.items():
            if value == "":
                c.execute("DELETE FROM tool_config WHERE key=?", (key,))
            else:
                c.execute(
                    "INSERT INTO tool_config (key, value) VALUES (?, ?) "
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
