"""Process-local locks for filesystem mutations.

Locks are keyed by normalized directory paths.  Multiple directories are acquired in
sorted order to avoid deadlocks; weak references keep the registry from growing for
ever after one-off operations.
"""
from __future__ import annotations

import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class PathLockManager:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: weakref.WeakValueDictionary[str, threading.RLock] = (
            weakref.WeakValueDictionary()
        )

    @staticmethod
    def _key(path: str | Path) -> str:
        value = Path(path).expanduser().resolve(strict=False)
        return str(value).casefold()

    def _get(self, key: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def acquire(self, *directories: str | Path) -> Iterator[None]:
        keys = sorted({self._key(value) for value in directories})
        locks = [self._get(key) for key in keys]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()


path_locks = PathLockManager()
