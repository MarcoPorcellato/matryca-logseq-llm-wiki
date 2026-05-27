"""Per-file write locks serializing Read-Modify-Write cycles across threads and processes."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from loguru import logger

from .io_retry import (
    IO_RETRY_ATTEMPTS,
    IO_RETRY_INITIAL_DELAY_S,
    IO_RETRY_MAX_DELAY_S,
    PageLockUnavailableError,
)

_fcntl: Any
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows and other non-Unix platforms
    _fcntl = None

_registry_guard = threading.Lock()
_page_locks: OrderedDict[str, threading.Lock] = OrderedDict()
_MAX_PAGE_LOCK_REGISTRY = 4096


def _flock_degradation_allowed() -> bool:
    raw = os.environ.get("MATRYCA_ALLOW_FLOCK_DEGRADATION", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def normalize_page_lock_key(page_path: str | Path) -> str:
    """Return a stable absolute path string used as the lock registry key."""
    return str(Path(page_path).expanduser().resolve(strict=False))


def _sidecar_lock_path(page_path: str | Path) -> Path:
    """Return a hidden lock file adjacent to the target (same directory, one volume)."""
    path = Path(page_path).expanduser().resolve(strict=False)
    return path.parent / f".{path.name}.matryca.lock"


def _lock_for_key(key: str) -> threading.Lock:
    with _registry_guard:
        lock = _page_locks.get(key)
        if lock is not None:
            _page_locks.move_to_end(key)
            return lock
        while len(_page_locks) >= _MAX_PAGE_LOCK_REGISTRY:
            evicted = False
            for old_key in list(_page_locks):
                old_lock = _page_locks[old_key]
                if not old_lock.locked():
                    del _page_locks[old_key]
                    evicted = True
                    break
            if not evicted:
                raise PageLockUnavailableError(
                    f"In-process page lock registry full ({_MAX_PAGE_LOCK_REGISTRY}); "
                    f"cannot register lock for {key}",
                )
        lock = threading.Lock()
        _page_locks[key] = lock
        return lock


def _acquire_cross_process_flock(fd: int, lock_path: Path) -> bool:
    """Acquire exclusive flock with backoff; return False when flock is unsupported."""
    delay = IO_RETRY_INITIAL_DELAY_S
    for attempt in range(IO_RETRY_ATTEMPTS):
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if attempt >= IO_RETRY_ATTEMPTS - 1:
                logger.warning(
                    "Page lock sidecar still held after {} retries: {}",
                    IO_RETRY_ATTEMPTS - 1,
                    lock_path,
                )
                raise PageLockUnavailableError(
                    f"Could not acquire page lock for {lock_path} after retries",
                ) from None
            time.sleep(min(IO_RETRY_MAX_DELAY_S, delay))
            delay = min(IO_RETRY_MAX_DELAY_S, delay * 2)
        except OSError as exc:
            if not _flock_degradation_allowed():
                raise PageLockUnavailableError(
                    f"Cross-process page lock unavailable for {lock_path}: {exc}",
                ) from exc
            logger.info(
                "[LOCK FILE SYSTEM DEGRADATION] Shared process lock not supported by "
                "filesystem ({}), falling back to pure in-process thread locking.",
                exc,
            )
            return False
    return False


@contextmanager
def _cross_process_file_lock(page_path: str | Path) -> Iterator[None]:
    """Exclusive ``fcntl.flock`` on a sidecar lock file (no-op when ``fcntl`` is unavailable)."""
    if _fcntl is None:
        yield
        return

    lock_path = _sidecar_lock_path(page_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        acquired = _acquire_cross_process_flock(fd, lock_path)
        if not acquired and _fcntl is not None and not _flock_degradation_allowed():
            raise PageLockUnavailableError(
                f"Could not acquire cross-process page lock for {lock_path}",
            )
        if not acquired:
            yield
            return
        try:
            yield
        finally:
            with suppress(OSError):
                _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)


def probe_page_rmw_lock(page_path: str | Path) -> None:
    """Verify the page lock can be acquired without holding it through long work.

    Raises:
        PageLockUnavailableError: When thread or cross-process locks are contended.
    """
    key = normalize_page_lock_key(page_path)
    thread_lock = _lock_for_key(key)
    if not thread_lock.acquire(blocking=False):
        raise PageLockUnavailableError(
            f"Could not probe in-process page lock for {page_path}",
        )
    try:
        if _fcntl is None:
            return
        lock_path = _sidecar_lock_path(page_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PageLockUnavailableError(
                    f"Could not probe cross-process page lock for {lock_path}",
                ) from exc
            except OSError as exc:
                if not _flock_degradation_allowed():
                    raise PageLockUnavailableError(
                        f"Cross-process page lock unavailable for {lock_path}: {exc}",
                    ) from exc
                return
            with suppress(OSError):
                _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        thread_lock.release()


@contextmanager
def page_rmw_lock(page_path: str | Path) -> Iterator[None]:
    """Hold an exclusive lock for one file's full RMW lifecycle (thread- and process-safe)."""
    key = normalize_page_lock_key(page_path)
    thread_lock = _lock_for_key(key)
    delay = IO_RETRY_INITIAL_DELAY_S
    for attempt in range(IO_RETRY_ATTEMPTS):
        if thread_lock.acquire(blocking=False):
            break
        if attempt >= IO_RETRY_ATTEMPTS - 1:
            logger.warning(
                "In-process page lock still held after {} retries: {}",
                IO_RETRY_ATTEMPTS - 1,
                page_path,
            )
            raise PageLockUnavailableError(
                f"Could not acquire in-process page lock for {page_path} after retries",
            )
        time.sleep(min(IO_RETRY_MAX_DELAY_S, delay))
        delay = min(IO_RETRY_MAX_DELAY_S, delay * 2)
    try:
        with _cross_process_file_lock(page_path):
            yield
    finally:
        thread_lock.release()


def clear_page_write_locks() -> None:
    """Drop the in-process lock registry (for tests)."""
    with _registry_guard:
        _page_locks.clear()


def sweep_matryca_lock_sidecars(graph_root: str | Path) -> int:
    """Remove orphan ``.{page}.matryca.lock`` sidecars under ``pages/`` and ``journals/``."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    removed = 0
    for sub in ("pages", "journals"):
        base = root / sub
        if not base.is_dir():
            continue
        for candidate in base.rglob("*.matryca.lock"):
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
                removed += 1
    return removed


def cross_process_lock_available() -> bool:
    """Return whether OS-level ``flock`` locking is active on this platform."""
    return _fcntl is not None and sys.platform != "win32"


__all__ = [
    "clear_page_write_locks",
    "cross_process_lock_available",
    "normalize_page_lock_key",
    "PageLockUnavailableError",
    "page_rmw_lock",
    "probe_page_rmw_lock",
    "sweep_matryca_lock_sidecars",
]
