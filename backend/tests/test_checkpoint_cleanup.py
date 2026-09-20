"""B4 回归：checkpoint 的 TTL 清理。

历史缺陷：``cleanup_checkpoints`` 里 ``cutoff`` 算完从未使用，``max_age_days``
完全是死代码 —— 实际只按 ``max_sessions`` 无条件截断，够旧的会话也一并删掉。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from app import db
from app.agent import graph as graph_module
from app.agent.state import PlanOutput, ReflectionOutput
from app.config import get_settings


class _FakeCheckpointer:
    """最小 checkpointer 替身：cleanup_checkpoints 只用到 .conn。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn


def _uuid6_at(moment: datetime) -> str:
    """按 langgraph uuid6() 的布局生成指定时刻的 checkpoint_id。

    60 位时间戳（自 1582-10-15 起的 100ns 计数）：高 48 位放在最前面，
    版本位之后接低 12 位，variant 置为 0b10。
    """
    intervals = int((moment - graph_module._UUID_EPOCH).total_seconds() * 10_000_000)
    high48 = (intervals >> 12) & 0xFFFFFFFFFFFF
    low12 = intervals & 0x0FFF
    return str(uuid.UUID(int=(high48 << 80) | (low12 << 64) | (0b10 << 62) | (6 << 76)))


def _db(entries: List[tuple[str, str]], *, with_writes: bool = True) -> sqlite3.Connection:
    """按 langgraph 的真实 schema 造库：主表 checkpoints，副表叫 writes（不是 checkpoint_writes）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)")
    if with_writes:
        conn.execute("CREATE TABLE writes (thread_id TEXT)")
    for thread_id, checkpoint_id in entries:
        conn.execute("INSERT INTO checkpoints VALUES (?, ?)", (thread_id, checkpoint_id))
        if with_writes:
            conn.execute("INSERT INTO writes VALUES (?)", (thread_id,))
    conn.commit()
    return conn


def _threads(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}


def _run(conn: sqlite3.Connection, *, max_sessions: int, max_age_days: int, states: Optional[Dict[str, Any]] = None) -> int:
    reader = (states or {}).get
    return graph_module.cleanup_checkpoints(
        _FakeCheckpointer(conn), reader, max_sessions=max_sessions, max_age_days=max_age_days
    )


# ---------------- id 解析 ----------------


def test_checkpoint_timestamp_roundtrip() -> None:
    """自造的 UUIDv6 id 必须能被解析回原时刻。"""
    for days in (0, 5, 45, 900):
        moment = datetime.now(timezone.utc) - timedelta(days=days)
        parsed = graph_module._checkpoint_created_at(_uuid6_at(moment))
        assert parsed is not None, f"{days} 天前的 id 解析失败"
        assert abs((parsed - moment).total_seconds()) < 1, f"偏差过大: {parsed} vs {moment}"


def test_checkpoint_timestamp_rejects_non_uuid6() -> None:
    """非 UUIDv6 一律返回 None，交由调用方走保守分支。"""
    for value in ("not-a-uuid", "", None, uuid.uuid4().hex, str(uuid.uuid1()), "12345"):
        assert graph_module._checkpoint_created_at(value) is None


# ---------------- 清理策略 ----------------


def test_old_sessions_beyond_cap_are_removed() -> None:
    """全部会话都已过期时，保留最近 N 个，其余按 TTL 删除。"""
    now = datetime.now(timezone.utc)
    conn = _db([
        ("keep-a", _uuid6_at(now - timedelta(days=400))),
        ("keep-b", _uuid6_at(now - timedelta(days=500))),
        ("drop-c", _uuid6_at(now - timedelta(days=600))),
    ])
    assert _run(conn, max_sessions=2, max_age_days=30) == 1
    assert _threads(conn) == {"keep-a", "keep-b"}
    # 副表必须一并清掉，否则残留孤儿行持续占用空间。
    writes = {row[0] for row in conn.execute("SELECT thread_id FROM writes")}
    assert writes == {"keep-a", "keep-b"}


def test_recent_sessions_are_kept_even_beyond_cap() -> None:
    """TTL 未到：即使超出 max_sessions 也不删（这正是此前被误删的部分）。"""
    now = datetime.now(timezone.utc)
    conn = _db([
        ("a", _uuid6_at(now - timedelta(minutes=1))),
        ("b", _uuid6_at(now - timedelta(minutes=2))),
        ("c", _uuid6_at(now - timedelta(hours=3))),
    ])
    assert _run(conn, max_sessions=1, max_age_days=30) == 0
    assert _threads(conn) == {"a", "b", "c"}


def test_only_expired_beyond_cap_are_removed() -> None:
    """混合场景：只有「超出保留数」且「已过期」的会话被删除。"""
    now = datetime.now(timezone.utc)
    conn = _db([
        ("newest", _uuid6_at(now - timedelta(minutes=1))),
        ("recent", _uuid6_at(now - timedelta(hours=1))),
        ("expired-1", _uuid6_at(now - timedelta(days=100))),
        ("expired-2", _uuid6_at(now - timedelta(days=800))),
    ])
    assert _run(conn, max_sessions=2, max_age_days=30) == 2
    assert _threads(conn) == {"newest", "recent"}


def test_expired_pending_approval_survives_by_status() -> None:
    """待审批会话即使已过期也必须保留，否则用户永远无法再批准。"""
    now = datetime.now(timezone.utc)
    conn = _db([
        ("newest", _uuid6_at(now - timedelta(minutes=1))),
        ("pending", _uuid6_at(now - timedelta(days=800))),
        ("expired", _uuid6_at(now - timedelta(days=900))),
    ])
    states = {"pending": {"status": "waiting_approval", "pending_approval": {"approval_id": "x"}}}
    # max_sessions=0：让保留名额不影响结果，唯一护住 pending 的只能是状态判定。
    assert _run(conn, max_sessions=0, max_age_days=30, states=states) == 1
    assert _threads(conn) == {"newest", "pending"}


def test_expired_pending_approval_survives_by_payload_only() -> None:
    """status 已不是 waiting_approval 但 pending_approval 还在，同样必须保留。"""
    now = datetime.now(timezone.utc)
    conn = _db([
        ("newest", _uuid6_at(now - timedelta(minutes=1))),
        ("pending", _uuid6_at(now - timedelta(days=800))),
    ])
    states = {"pending": {"status": "running", "pending_approval": {"approval_id": "x"}}}
    assert _run(conn, max_sessions=0, max_age_days=30, states=states) == 0
    assert _threads(conn) == {"newest", "pending"}


def test_state_reader_failure_still_cleans() -> None:
    """state_reader 抛错时按“无待审批”处理并照常清理。

    这是 B4 之前的既有行为（``except Exception: state = {}``），本次未改动：
    读不出状态时无法判定是否有待审批，代价是极小概率误删一个待审批会话。
    记录在案，属于已知残余风险而非回归。
    """

    def _boom(_thread_id: str) -> Dict[str, Any]:
        raise RuntimeError("checkpoint 读取失败")

    now = datetime.now(timezone.utc)
    conn = _db([
        ("a", _uuid6_at(now - timedelta(days=800))),
        ("b", _uuid6_at(now - timedelta(days=900))),
    ])
    assert graph_module.cleanup_checkpoints(
        _FakeCheckpointer(conn), _boom, max_sessions=0, max_age_days=30
    ) == 2
    assert _threads(conn) == set()


def test_unparseable_checkpoint_id_is_kept() -> None:
    """id 解析不出时间时宁可保留：清理是破坏性操作，失败要偏保守。"""
    conn = _db([("weird", "not-a-uuid"), ("normal", _uuid6_at(datetime.now(timezone.utc) - timedelta(days=900)))])
    assert _run(conn, max_sessions=0, max_age_days=30) == 1
    assert _threads(conn) == {"weird"}


def test_missing_connection_is_noop() -> None:
    """拿不到连接时安静返回 0，不得抛错（进程启动路径依赖它）。"""

    class _NoConn:
        pass

    assert graph_module.cleanup_checkpoints(_NoConn(), lambda _tid: {}) == 0


def test_missing_writes_table_still_deletes_checkpoints() -> None:
    """副表缺失时仍要能删主表。

    这正是旧实现的致命处：DELETE 写错表名抛 no such table，被 except 吞掉后 rollback，
    于是删除从未生效 —— 整个 cleanup 是空操作。
    """
    now = datetime.now(timezone.utc)
    conn = _db([
        ("newest", _uuid6_at(now - timedelta(minutes=1))),
        ("expired", _uuid6_at(now - timedelta(days=900))),
    ], with_writes=False)
    assert _run(conn, max_sessions=1, max_age_days=30) == 1
    assert _threads(conn) == {"newest"}


def test_uninitialized_database_is_noop() -> None:
    """库还没建表时安静返回 0（首次启动 cleanup 早于首次落库）。"""
    conn = sqlite3.connect(":memory:")
    assert _run(conn, max_sessions=0, max_age_days=30) == 0


# ---------------- 与真实 langgraph 产物的对照 ----------------


class _EmptyPlanner:
    def invoke(self, _messages: List[Any]) -> PlanOutput:
        return PlanOutput(goal="done", steps=[], user_message="完成")


class _Reflector:
    def invoke(self, _messages: List[Any]) -> ReflectionOutput:
        return ReflectionOutput(decision="done", reasoning="done", final_response="done")


class _LLM:
    def with_structured_output(self, schema: Any) -> Any:
        return _EmptyPlanner() if schema is PlanOutput else _Reflector()

    def invoke(self, _: Any) -> Any:  # pragma: no cover
        raise AssertionError


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SANDBOX_ROOTS", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "butler.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    get_settings.cache_clear()
    db._initialized = False
    db.init_db()
    monkeypatch.setattr(graph_module, "build_llm", lambda temperature=0.1: _LLM())
    yield tmp_path
    get_settings.cache_clear()
    db._initialized = False


def test_checkpoint_created_at_matches_real_langgraph_ids(env: Path) -> None:
    """关键对照：真实 langgraph 生成的 checkpoint_id 必须能被解出“刚刚”。

    这条测试是 B4 的地基 —— 若 id 的编码假设有误，TTL 会静默失效成新的死代码。
    """
    runtime = graph_module.AgentRuntime()
    assert getattr(runtime.checkpointer, "conn", None) is not None, "拿不到连接，清理逻辑会静默失效"
    for _ in runtime.start_stream("归档", "real-1"):
        pass

    rows = runtime.checkpointer.conn.execute(
        "SELECT DISTINCT thread_id, MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id"
    ).fetchall()
    assert rows, "没有产生任何 checkpoint，对照无效"

    now = datetime.now(timezone.utc)
    for thread_id, checkpoint_id in rows:
        parsed = graph_module._checkpoint_created_at(checkpoint_id)
        assert parsed is not None, f"无法解析真实 checkpoint_id: {thread_id}={checkpoint_id}"
        assert abs((now - parsed).total_seconds()) < 600, f"{thread_id} 解析时间偏差过大: {parsed}"


def test_real_checkpoint_ids_sort_chronologically(env: Path) -> None:
    """清理依赖「MAX(checkpoint_id) 即最新、字符串倒序即时间倒序」，需实测确认。"""
    runtime = graph_module.AgentRuntime()
    for index in range(3):
        for _ in runtime.start_stream("归档", f"real-{index}"):
            pass

    ids = [row[0] for row in runtime.checkpointer.conn.execute(
        "SELECT MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id ORDER BY MAX(checkpoint_id) DESC"
    ).fetchall()]
    times = [graph_module._checkpoint_created_at(cid) for cid in ids]
    assert all(t is not None for t in times)
    assert times == sorted(times, reverse=True), "字符串倒序与时间倒序不一致，清理会误删"


def test_cleanup_actually_deletes_from_real_langgraph_db(env: Path) -> None:
    """端到端：在真实 langgraph 落库的 schema 上，删除必须真的生效。

    旧实现在真实库上 100% 空操作（表名写错 → DELETE 抛错 → 回滚），
    因此这条测试才是 B4「TTL 真的会删」的最终凭据。
    """
    runtime = graph_module.AgentRuntime()
    conn = runtime.checkpointer.conn
    for index in range(3):
        for _ in runtime.start_stream("归档", f"real-{index}"):
            pass
    assert conn.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints").fetchone()[0] == 3

    # TTL=0：刚落盘的 checkpoint 也已“过期”，max_sessions=0 让保留名额不生效。
    removed = graph_module.cleanup_checkpoints(
        runtime.checkpointer, lambda _tid: {}, max_sessions=0, max_age_days=0
    )
    assert removed == 3, "清理未能真正删除会话（表名/事务问题会让它静默变成空操作）"
    assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 0
