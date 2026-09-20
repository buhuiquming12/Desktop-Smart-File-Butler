"""B9 回归：SQLite 连接的 PRAGMA 设置。

历史缺陷：``_conn`` 每条连接都重复执行 ``PRAGMA journal_mode=WAL``。该属性写在库头
里、是持久设置，重复设置只会让每次连接都去抢一次写锁；更麻烦的是它排在
``busy_timeout`` 之前 —— 抢不到锁时没有等待时长，直接抛 "database is locked"。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, List

import pytest

from app import db
from app.config import get_settings


class _TracingConnection:
    """记录 execute 过的语句并转发给真实连接。

    sqlite3.Connection 是 C 类型、不允许挂实例属性，因此这里用代理而不是子类。
    db._conn 只用到了 execute / commit / close / row_factory。
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.statements: List[str] = []

    def execute(self, sql: str, *args: Any) -> Any:
        self.statements.append(sql)
        return self._real.execute(sql, *args)

    def executescript(self, sql: str) -> Any:
        self.statements.append(sql)
        return self._real.executescript(sql)

    def commit(self) -> None:
        self._real.commit()

    def close(self) -> None:
        self._real.close()

    @property
    def row_factory(self) -> Any:
        return self._real.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._real.row_factory = value


@pytest.fixture()
def traced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> List[_TracingConnection]:
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    made: List[_TracingConnection] = []
    real_connect = sqlite3.connect

    def _connect(*args: Any, **kwargs: Any) -> Any:
        proxy = _TracingConnection(real_connect(*args, **kwargs))
        made.append(proxy)
        return proxy

    monkeypatch.setattr(db.sqlite3, "connect", _connect)
    yield made
    get_settings.cache_clear()
    db._initialized = False


def _conn_statements(traced: List[_TracingConnection], count: int) -> List[str]:
    """取最近 count 次 _conn() 各自执行的语句（每次连接产出一个代理）。"""
    return traced[-count].statements


def test_wal_is_set_once_at_init(traced: List[_TracingConnection]) -> None:
    """init_db 必须把库切到 WAL；此后每条连接不得再重复设置。"""
    db.init_db()
    init_sql = " ".join(s.lower() for c in traced for s in c.statements)
    assert "journal_mode=wal" in init_sql, "建库时未设置 WAL"

    before = len(traced)
    with db._conn() as conn:
        conn.execute("SELECT 1")
    assert len(traced) == before + 1, "预期只新建一条连接"
    per_conn = " ".join(s.lower() for s in _conn_statements(traced, 1))
    assert "journal_mode" not in per_conn, "每条连接都在重复设置 WAL（多余的写锁争用）"


def test_wal_persists_across_connections(traced: List[_TracingConnection]) -> None:
    """WAL 是持久属性：换一条全新连接打开，模式仍是 wal。"""
    db.init_db()
    with db._conn() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_busy_timeout_is_set_before_lock_taking_pragma(traced: List[_TracingConnection]) -> None:
    """busy_timeout 必须先于 journal_mode：抢锁前得先有等待时长，否则直接报锁错误。"""
    db.init_db()
    statements = [s.lower() for c in traced for s in c.statements]
    timeout_at = next(i for i, s in enumerate(statements) if "busy_timeout" in s)
    journal_at = next(i for i, s in enumerate(statements) if "journal_mode" in s)
    assert timeout_at < journal_at, "journal_mode 早于 busy_timeout，锁冲突时会直接失败"


def test_per_connection_pragmas_still_applied(traced: List[_TracingConnection]) -> None:
    """synchronous 是连接级设置，每条连接都必须重新设置。"""
    db.init_db()
    before = len(traced)
    with db._conn() as conn:
        conn.execute("SELECT 1")
    statements = [s.lower() for s in _conn_statements(traced, len(traced) - before)]
    assert any("synchronous" in s for s in statements)
    assert any("busy_timeout" in s for s in statements)
