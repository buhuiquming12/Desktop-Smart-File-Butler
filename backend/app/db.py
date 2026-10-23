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

                -- P0：操作预览。items_json 保存逐文件计划，digest 用于检测预览失效，
                -- approval_json 保存“这一次确认”绑定的条目集合与最终落点，避免确认被
                -- 复用扩大或执行前临时改目标；model_calls 记录本次预览真实消耗的模型
                -- 调用次数（用于与运行预算对账，缺列时补 0）。
                CREATE TABLE IF NOT EXISTS operation_previews (
                    preview_id  TEXT PRIMARY KEY,
                    created_at  TEXT NOT NULL,
                    op          TEXT NOT NULL,
                    directory   TEXT NOT NULL,
                    spec_json   TEXT NOT NULL,
                    items_json  TEXT NOT NULL,
                    digest      TEXT NOT NULL,
                    approval_json TEXT DEFAULT '',
                    thread_id   TEXT,
                    status      TEXT NOT NULL DEFAULT 'preview',
                    model_calls INTEGER NOT NULL DEFAULT 0
                );

                -- P2：内容索引。content 只保存提取出的正文（不是原始文档），并用
                -- truncated/status 明确标记截断与失败，避免把“没索引到”误当“没内容”。
                CREATE TABLE IF NOT EXISTS content_index (
                    path       TEXT PRIMARY KEY,
                    root       TEXT NOT NULL,
                    size       INTEGER NOT NULL DEFAULT 0,
                    mtime_ns   INTEGER NOT NULL DEFAULT 0,
                    ext        TEXT NOT NULL DEFAULT '',
                    status     TEXT NOT NULL DEFAULT 'indexed',
                    error      TEXT DEFAULT '',
                    content    TEXT DEFAULT '',
                    truncated  INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_content_index_root ON content_index(root);

                -- P3：内容分类缓存；键包含 size/mtime_ns，文件一变缓存即失效。
                CREATE TABLE IF NOT EXISTS classification_cache (
                    path       TEXT PRIMARY KEY,
                    size       INTEGER NOT NULL,
                    mtime_ns   INTEGER NOT NULL,
                    mode       TEXT NOT NULL,
                    category   TEXT NOT NULL,
                    basis      TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                -- P5：自动整理规则与已处理事件（用于重启后补扫与去重）。
                CREATE TABLE IF NOT EXISTS organize_rules (
                    rule_id         TEXT PRIMARY KEY,
                    directory       TEXT NOT NULL,
                    dest_root       TEXT NOT NULL,
                    mode            TEXT NOT NULL DEFAULT 'extension',
                    instruction     TEXT NOT NULL DEFAULT '',
                    enabled         INTEGER NOT NULL DEFAULT 1,
                    settle_seconds  INTEGER NOT NULL DEFAULT 5,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    last_run_at     TEXT DEFAULT '',
                    last_result     TEXT DEFAULT '',
                    processed_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS watch_events (
                    rule_id      TEXT NOT NULL,
                    path         TEXT NOT NULL,
                    signature    TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    detail       TEXT DEFAULT '',
                    UNIQUE(rule_id, path, signature)
                );
                """
            )
            # FTS5 是可选能力：某些发行版没有编译 fts5。缺它时搜索回退到子串匹配，
            # 而不是让整个 init_db 失败。
            try:
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS content_index_fts "
                    "USING fts5(path UNINDEXED, body)"
                )
            except sqlite3.OperationalError:
                logger.warning("SQLite 未启用 FTS5，内容搜索将使用子串匹配")

            # 迁移：为老用户已存在的 operation_log 补 thread_id 列（CREATE IF NOT EXISTS
            # 不会给旧表加列）。新增可空列对既有行安全，值为 NULL（见 P1-1）。
            columns = {row["name"] for row in c.execute("PRAGMA table_info(operation_log)")}
            if "thread_id" not in columns:
                c.execute("ALTER TABLE operation_log ADD COLUMN thread_id TEXT")
                logger.info("operation_log 迁移：已新增 thread_id 列")
            # 迁移：预览的模型调用计数列（老库没有该列，补 0 而非 NULL）。
            preview_columns = {
                row["name"] for row in c.execute("PRAGMA table_info(operation_previews)")
            }
            if "model_calls" not in preview_columns:
                c.execute(
                    "ALTER TABLE operation_previews ADD COLUMN model_calls INTEGER NOT NULL DEFAULT 0"
                )
                logger.info("operation_previews 迁移：已新增 model_calls 列")
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


def delete_preference(key: str) -> bool:
    """删除普通偏好；保留键（``__`` 前缀）不可通过该入口删除。"""
    normalized = validate_public_preference_key(key)
    with _conn() as c:
        cursor = c.execute("DELETE FROM preferences WHERE key=?", (normalized,))
    return cursor.rowcount > 0


def all_preferences() -> List[Preference]:
    """返回用户偏好。以 __ 开头的保留键（如内部沙箱配置）不对外暴露、也不喂给规划器。

    密钥类键（历史版本可能误存）同样不返回：密钥只能存在于 SecretStore。
    """
    from .security.secrets import is_secret_name

    with _conn() as c:
        rows = c.execute("SELECT key, value FROM preferences").fetchall()
    return [
        Preference(key=r["key"], value=r["value"])
        for r in rows
        if not r["key"].startswith("__") and not is_secret_name(str(r["key"]))
    ]


# ---------- 模型配置（前端可写，覆盖 .env 默认值） ----------

def get_llm_config() -> dict[str, str]:
    """返回所有可安全展示的模型配置覆盖项（键值对）。

    **绝不返回密钥类字段**：历史版本曾把 API Key 明文写进 ``llm_config``，
    这里统一过滤，避免任何调用方（接口、日志、诊断、规划器）顺手把明文带出去。
    迁移逻辑通过 ``get_llm_config_value`` 单独读取旧值。
    """
    from .security.secrets import is_secret_name

    with _conn() as c:
        rows = c.execute("SELECT key, value FROM llm_config").fetchall()
    return {
        r["key"]: r["value"]
        for r in rows
        if not is_secret_name(str(r["key"]))
    }


def get_llm_config_value(key: str) -> Optional[str]:
    """读取单个配置原始值（含历史明文密钥），仅供受控迁移代码使用。"""
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM llm_config WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else None


def delete_llm_config_key(key: str) -> bool:
    """删除单个配置覆盖项，返回是否确有删除（用于迁移成功后清理明文）。"""
    with _conn() as c:
        cursor = c.execute("DELETE FROM llm_config WHERE key=?", (key,))
    return cursor.rowcount > 0


def scrub_llm_config_plaintext(keys: tuple[str, ...] = ("openai_api_key",)) -> None:
    """彻底抹掉 ``llm_config`` 里历史明文密钥的残留字节。

    仅 ``DELETE`` 行是不够的：SQLite 只把页标记为空闲，旧内容仍留在数据库文件
    （以及 WAL）里，用文本搜索就能捞回密钥。因此这里：

    1. 用 ``PRAGMA secure_delete=ON`` 的连接删除残留行（删除时把内容清零）；
    2. 执行 ``VACUUM`` 重写整库，丢弃空闲页；
    3. ``wal_checkpoint(TRUNCATE)`` 清空 WAL，避免旧页仍留在旁文件里。

    该函数必须在没有活动事务的独立连接上执行（VACUUM 不能在事务内运行）。
    任何失败都只记警告：清理失败不应阻止迁移成功后的正常使用，但会留下诊断。
    """
    import sqlite3 as _sqlite3

    settings = get_settings()
    try:
        conn = _sqlite3.connect(settings.db_path, timeout=10, isolation_level=None)
    except _sqlite3.Error as exc:
        logger.warning("无法打开数据库以清理历史明文密钥残留: %s", type(exc).__name__)
        return
    try:
        conn.execute("PRAGMA secure_delete=ON")
        for key in keys:
            conn.execute("DELETE FROM llm_config WHERE key=?", (key,))
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except _sqlite3.Error as exc:
        logger.warning("清理历史明文密钥残留失败（不影响已完成的迁移）: %s", type(exc).__name__)
    finally:
        conn.close()


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


# ---------- 通用连接（供索引/复检等需要事务的模块复用） ----------

def connection() -> Iterator[sqlite3.Connection]:
    """公开的连接上下文管理器：模块内私有 ``_conn`` 的稳定别名。

    索引器需要把“写正文 + 写全文索引”放在同一个事务里，因此必须复用同一连接。
    """
    return _conn()


# ---------- 操作预览（P0） ----------

def save_preview(record: dict) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO operation_previews"
            " (preview_id, created_at, op, directory, spec_json, items_json, digest,"
            "  approval_json, thread_id, status, model_calls)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["preview_id"],
                record["created_at"],
                record["op"],
                record["directory"],
                record["spec_json"],
                record["items_json"],
                record["digest"],
                record.get("approval_json", ""),
                record.get("thread_id"),
                record.get("status", "preview"),
                int(record.get("model_calls") or 0),
            ),
        )


def get_preview(preview_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM operation_previews WHERE preview_id=?", (preview_id,)
        ).fetchone()
    if row is None:
        return None
    keys = row.keys()
    return {
        "preview_id": row["preview_id"],
        "created_at": row["created_at"],
        "op": row["op"],
        "directory": row["directory"],
        "spec": json.loads(row["spec_json"]),
        "items": json.loads(row["items_json"]),
        "digest": row["digest"],
        "approval": json.loads(row["approval_json"]) if row["approval_json"] else None,
        "thread_id": row["thread_id"],
        "status": row["status"],
        "model_calls": int(row["model_calls"] or 0) if "model_calls" in keys else 0,
    }


def list_previews(limit: int = 20) -> List[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT preview_id, created_at, op, directory, digest, status"
            " FROM operation_previews ORDER BY created_at DESC LIMIT ?",
            (max(1, min(200, limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def update_preview(preview_id: str, **fields) -> None:
    allowed = {"approval_json", "status", "items_json", "digest"}
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key}=?" for key in values)
    with _conn() as c:
        c.execute(
            f"UPDATE operation_previews SET {assignments} WHERE preview_id=?",
            (*values.values(), preview_id),
        )


def claim_preview_execution(preview_id: str) -> bool:
    """把预览状态从 ``preview`` 原子地抢到 ``executing``。

    返回 False 表示已经被执行过或正在执行：确认（approval token）只能使用一次，
    文件恢复原位后也不能拿旧 token 再执行一遍。
    """
    with _conn() as c:
        cursor = c.execute(
            "UPDATE operation_previews SET status='executing'"
            " WHERE preview_id=? AND status='preview'",
            (preview_id,),
        )
        return cursor.rowcount > 0


def release_preview_execution(preview_id: str) -> None:
    """核验失败时把抢占还给用户：状态回到 ``preview``，可以重新确认。"""
    with _conn() as c:
        c.execute(
            "UPDATE operation_previews SET status='preview'"
            " WHERE preview_id=? AND status='executing'",
            (preview_id,),
        )


# ---------- 内容分类缓存（P3） ----------

def get_classification(path: str, size: int, mtime_ns: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM classification_cache WHERE path=?", (path,)
        ).fetchone()
    if row is None or int(row["size"]) != size or int(row["mtime_ns"]) != mtime_ns:
        return None
    return {"category": row["category"], "basis": row["basis"], "mode": row["mode"]}


def set_classification(
    path: str, size: int, mtime_ns: int, mode: str, category: str, basis: str
) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO classification_cache"
            " (path, size, mtime_ns, mode, category, basis, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET size=excluded.size,"
            " mtime_ns=excluded.mtime_ns, mode=excluded.mode,"
            " category=excluded.category, basis=excluded.basis,"
            " created_at=excluded.created_at",
            (path, size, mtime_ns, mode, category, basis,
             datetime.now(timezone.utc).isoformat()),
        )


# ---------- 自动整理规则（P5） ----------

def list_rules() -> List[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM organize_rules ORDER BY created_at ASC").fetchall()
    return [dict(row) for row in rows]


def get_rule(rule_id: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM organize_rules WHERE rule_id=?", (rule_id,)).fetchone()
    return dict(row) if row else None


def upsert_rule(rule: dict) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO organize_rules"
            " (rule_id, directory, dest_root, mode, instruction, enabled, settle_seconds,"
            "  created_at, updated_at, last_run_at, last_result, processed_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(rule_id) DO UPDATE SET directory=excluded.directory,"
            " dest_root=excluded.dest_root, mode=excluded.mode,"
            " instruction=excluded.instruction, enabled=excluded.enabled,"
            " settle_seconds=excluded.settle_seconds, updated_at=excluded.updated_at",
            (
                rule["rule_id"], rule["directory"], rule["dest_root"], rule["mode"],
                rule.get("instruction", ""), int(bool(rule.get("enabled", True))),
                int(rule.get("settle_seconds", 5)), rule["created_at"], rule["updated_at"],
                rule.get("last_run_at", ""), rule.get("last_result", ""),
                int(rule.get("processed_count", 0)),
            ),
        )


def update_rule_runtime(rule_id: str, *, last_run_at: str, last_result: str,
                        processed_delta: int = 0) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE organize_rules SET last_run_at=?, last_result=?,"
            " processed_count=processed_count+? WHERE rule_id=?",
            (last_run_at, last_result[:500], int(processed_delta), rule_id),
        )


def delete_rule(rule_id: str) -> bool:
    with _conn() as c:
        cursor = c.execute("DELETE FROM organize_rules WHERE rule_id=?", (rule_id,))
        c.execute("DELETE FROM watch_events WHERE rule_id=?", (rule_id,))
    return cursor.rowcount > 0


def has_watch_event(rule_id: str, path: str, signature: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM watch_events WHERE rule_id=? AND path=? AND signature=?",
            (rule_id, path, signature),
        ).fetchone()
    return row is not None


def record_watch_event(rule_id: str, path: str, signature: str, detail: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO watch_events (rule_id, path, signature, processed_at, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (rule_id, path, signature, datetime.now(timezone.utc).isoformat(), detail[:500]),
        )


# ---------- 回收站（P6） ----------

def trashed_operations(limit: int = 500) -> List[OperationLog]:
    """已成功软删除的操作；是否仍在回收站由调用方按 dest 是否存在判定。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM operation_log WHERE action='delete' AND status='ok'"
            " AND dest IS NOT NULL AND dest != '' ORDER BY id DESC LIMIT ?",
            (max(1, min(2000, limit)),),
        ).fetchall()
    return [_row_to_operation(r) for r in rows]
