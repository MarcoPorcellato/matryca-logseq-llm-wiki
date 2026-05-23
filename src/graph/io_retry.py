"""Shared exponential backoff for OS-level I/O contention (cloud sync file locks)."""

from __future__ import annotations

import time
from collections.abc import Callable

from loguru import logger

IO_RETRY_ATTEMPTS = 4  # initial try + 3 retries
IO_RETRY_INITIAL_DELAY_S = 0.5
IO_RETRY_MAX_DELAY_S = 5.0


class PageLockUnavailableError(OSError):
    """Raised when an exclusive page lock cannot be acquired after retries."""


def retry_io_call[T](
    operation: Callable[[], T],
    *,
    description: str,
    attempts: int = IO_RETRY_ATTEMPTS,
    retry_on: tuple[type[BaseException], ...] = (BlockingIOError, OSError),
) -> T:
    """Run ``operation`` with exponential backoff when transient I/O errors occur."""
    delay = IO_RETRY_INITIAL_DELAY_S
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except retry_on as exc:
            last_exc = exc
            if attempt >= attempts - 1:
                break
            logger.warning(
                "I/O retry {}/{} for {} ({}): {}",
                attempt + 1,
                attempts - 1,
                description,
                type(exc).__name__,
                exc,
            )
            time.sleep(min(IO_RETRY_MAX_DELAY_S, delay))
            delay = min(IO_RETRY_MAX_DELAY_S, delay * 2)
    assert last_exc is not None
    raise last_exc


__all__ = [
    "IO_RETRY_ATTEMPTS",
    "IO_RETRY_INITIAL_DELAY_S",
    "IO_RETRY_MAX_DELAY_S",
    "PageLockUnavailableError",
    "retry_io_call",
]
