"""PyPI release checker for Matryca Plumber guided updates."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from ..graph.page_properties import get_plumber_version

_PYPI_JSON_URL = "https://pypi.org/pypi/matryca-logseq/json"
_PYPI_PROJECT_URL = "https://pypi.org/project/matryca-logseq/"
_REQUEST_TIMEOUT_SECONDS = 5.0
_CACHE_TTL_SECONDS = 3600.0

_cache: UpdateCheckResult | None = None
_cache_expires_at: float = 0.0


@dataclass(frozen=True, slots=True)
class UpdateCheckResult:
    """Outcome of comparing the installed package against PyPI."""

    current_version: str
    latest_version: str
    update_available: bool
    pypi_url: str


def _version_key(version: str) -> tuple[int, ...]:
    normalized = version.strip().lstrip("v").split("+", 1)[0]
    parts: list[int] = []
    for segment in re.split(r"[.-]", normalized):
        if segment.isdigit():
            parts.append(int(segment))
        else:
            break
    return tuple(parts or [0])


def _is_newer(latest: str, current: str) -> bool:
    if latest.strip().lower() == "unknown" or current.strip().lower() == "unknown":
        return False
    return _version_key(latest) > _version_key(current)


def _build_result(*, latest_version: str) -> UpdateCheckResult:
    current_version = get_plumber_version()
    return UpdateCheckResult(
        current_version=current_version,
        latest_version=latest_version,
        update_available=_is_newer(latest_version, current_version),
        pypi_url=_PYPI_PROJECT_URL,
    )


async def _fetch_latest_pypi_version() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(_PYPI_JSON_URL)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("PyPI update check failed: {}", exc)
        return None

    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    return version.strip()


async def check_for_updates(*, force_refresh: bool = False) -> UpdateCheckResult:
    """Compare the installed package version against the latest PyPI release."""
    global _cache, _cache_expires_at

    now = time.monotonic()
    if not force_refresh and _cache is not None and now < _cache_expires_at:
        return _cache

    current_version = get_plumber_version()
    latest_version = await _fetch_latest_pypi_version()
    if latest_version is None:
        result = _build_result(latest_version=current_version)
    else:
        result = _build_result(latest_version=latest_version)

    _cache = result
    _cache_expires_at = now + _CACHE_TTL_SECONDS
    return result


def clear_update_check_cache() -> None:
    """Reset the in-memory PyPI cache (used by tests)."""
    global _cache, _cache_expires_at
    _cache = None
    _cache_expires_at = 0.0


def update_check_to_dict(result: UpdateCheckResult) -> dict[str, Any]:
    """Serialize ``UpdateCheckResult`` for JSON responses."""
    return {
        "current_version": result.current_version,
        "latest_version": result.latest_version,
        "update_available": result.update_available,
        "pypi_url": result.pypi_url,
    }


__all__ = [
    "UpdateCheckResult",
    "check_for_updates",
    "clear_update_check_cache",
    "update_check_to_dict",
]
