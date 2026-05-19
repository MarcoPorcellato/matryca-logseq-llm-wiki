"""Per-file write locks serializing Read-Modify-Write cycles across worker threads."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_registry_guard = threading.Lock()
_page_locks: dict[str, threading.Lock] = {}


def normalize_page_lock_key(page_path: str | Path) -> str:
    """Return a stable absolute path string used as the lock registry key."""
    return str(Path(page_path).expanduser().resolve(strict=False))


def _lock_for_key(key: str) -> threading.Lock:
    with _registry_guard:
        lock = _page_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _page_locks[key] = lock
        return lock


@contextmanager
def page_rmw_lock(page_path: str | Path) -> Iterator[None]:
    """Hold an exclusive lock for one page's full RMW lifecycle (thread-safe)."""
    key = normalize_page_lock_key(page_path)
    lock = _lock_for_key(key)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def clear_page_write_locks() -> None:
    """Drop the lock registry (for tests)."""
    with _registry_guard:
        _page_locks.clear()


__all__ = [
    "clear_page_write_locks",
    "normalize_page_lock_key",
    "page_rmw_lock",
]
