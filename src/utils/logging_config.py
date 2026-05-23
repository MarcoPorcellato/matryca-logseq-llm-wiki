"""Central Loguru configuration for Matryca Plumber (rotation + retention)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

_CONFIGURED = False
_DEFAULT_LOG_DIR = Path("logs")
_DEFAULT_LOG_FILE = _DEFAULT_LOG_DIR / "matryca_plumber.log"


def resolve_loguru_log_path() -> Path:
    """Resolve the rotating Loguru file sink path from env or the default location."""
    override = os.environ.get("MATRYCA_LOGURU_LOG_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_LOG_FILE


def configure_loguru(*, level: str = "INFO", stderr: bool = True) -> None:
    """Register Loguru sinks once with enterprise rotation/retention (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    logger.remove()
    if stderr:
        logger.add(
            sys.stderr,
            level=level,
            colorize=True,
            backtrace=False,
            diagnose=False,
        )

    log_path = resolve_loguru_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_path),
        level=level,
        rotation="10 MB",
        retention="5",
        compression="zip",
        encoding="utf-8",
        backtrace=False,
        diagnose=False,
    )
    _CONFIGURED = True


__all__ = ["configure_loguru", "resolve_loguru_log_path"]
