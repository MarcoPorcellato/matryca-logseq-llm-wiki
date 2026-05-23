"""Cross-process exclusive flock for JSON checkpoint files."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

_fcntl: Any
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None


def flock_sidecar_path(target: Path) -> Path:
    """Return a sidecar lock path adjacent to ``target``."""
    return target.parent / f".{target.name}.flock"


@contextmanager
def cross_process_json_flock(target: Path) -> Iterator[None]:
    """Hold an exclusive flock for one JSON read/write critical section."""
    if _fcntl is None:
        yield
        return

    lock_path = flock_sidecar_path(target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        try:
            yield
        finally:
            with suppress(OSError):
                _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = ["cross_process_json_flock", "flock_sidecar_path"]
