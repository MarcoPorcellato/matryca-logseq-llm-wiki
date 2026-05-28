"""Cooperative CPU/disk yielding so long graph scans stay host-responsive."""

from __future__ import annotations

import os
import time

from .plumber_config import _env_float, _env_int

_YIELD_EVERY_ENV = "MATRYCA_BOOTSTRAP_YIELD_EVERY"
_YIELD_SLEEP_MS_ENV = "MATRYCA_YIELD_SLEEP_MS"
_IO_BATCH_PAUSE_MS_ENV = "MATRYCA_BOOTSTRAP_IO_BATCH_PAUSE_MS"

_DEFAULT_YIELD_EVERY = 25
_DEFAULT_YIELD_SLEEP_MS = 0.0
_DEFAULT_IO_BATCH_PAUSE_MS = 2.0


def bootstrap_yield_every() -> int:
    return max(1, _env_int(_YIELD_EVERY_ENV, _DEFAULT_YIELD_EVERY))


def yield_sleep_seconds() -> float:
    return max(0.0, _env_float(_YIELD_SLEEP_MS_ENV, _DEFAULT_YIELD_SLEEP_MS) / 1000.0)


def io_batch_pause_seconds() -> float:
    return max(0.0, _env_float(_IO_BATCH_PAUSE_MS_ENV, _DEFAULT_IO_BATCH_PAUSE_MS) / 1000.0)


def yield_host(
    batch_index: int,
    *,
    every_n: int | None = None,
    sleep_s: float | None = None,
) -> None:
    """Yield to the OS every ``every_n`` iterations (0-based ``batch_index``)."""
    interval = every_n if every_n is not None else bootstrap_yield_every()
    if interval <= 0:
        return
    if (batch_index + 1) % interval != 0:
        return
    delay = yield_sleep_seconds() if sleep_s is None else max(0.0, sleep_s)
    if delay > 0:
        time.sleep(delay)
    else:
        time.sleep(0)


def bootstrap_checkpoint_every() -> int:
    raw = os.environ.get("MATRYCA_BOOTSTRAP_CHECKPOINT_EVERY", "").strip()
    if not raw:
        return 50
    try:
        return max(1, int(raw))
    except ValueError:
        return 50


__all__ = [
    "bootstrap_checkpoint_every",
    "bootstrap_yield_every",
    "io_batch_pause_seconds",
    "yield_host",
    "yield_sleep_seconds",
]
