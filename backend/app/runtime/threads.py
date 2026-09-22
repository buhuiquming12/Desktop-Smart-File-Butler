"""活动 thread 的串行锁、引用计数与取消登记。"""
from __future__ import annotations

import asyncio
import weakref
from typing import Any, Callable, Dict, Set

from ..logging_conf import get_logger

logger = get_logger(__name__)

cancel_requested: Set[str] = set()
active_threads: Dict[str, int] = {}
thread_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def thread_lock(thread_id: str) -> asyncio.Lock:
    lock = thread_locks.get(thread_id)
    if lock is None:
        lock = asyncio.Lock()
        thread_locks[thread_id] = lock
    return lock


def register(thread_id: str) -> None:
    active_threads[thread_id] = active_threads.get(thread_id, 0) + 1


def unregister(thread_id: str) -> None:
    remaining = active_threads.get(thread_id, 0) - 1
    if remaining > 0:
        active_threads[thread_id] = remaining
    else:
        active_threads.pop(thread_id, None)


def request_cancel(thread_id: str, state_reader: Callable[[str], Dict[str, Any]]) -> bool:
    """只给真实活动或待审批会话登记取消，避免陈旧标记误伤未来请求。"""
    if active_threads.get(thread_id, 0) > 0:
        cancel_requested.add(thread_id)
        return True
    try:
        state = state_reader(thread_id)
    except Exception:  # noqa: BLE001
        logger.exception("检查待取消会话失败 thread=%s", thread_id)
        return False
    if state.get("status") in {"perceiving", "planning", "running", "waiting_approval"}:
        cancel_requested.add(thread_id)
        return True
    return False


def clear() -> None:
    thread_locks.clear()
    active_threads.clear()
    cancel_requested.clear()
