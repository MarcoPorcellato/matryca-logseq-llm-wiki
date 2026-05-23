"""Tests for the PyPI guided-update checker."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from src.graph.page_properties import get_plumber_version
from src.utils.updater import (
    UpdateCheckResult,
    _is_newer,
    check_for_updates,
    clear_update_check_cache,
    update_check_to_dict,
)


@pytest.fixture(autouse=True)
def _reset_update_cache() -> None:
    clear_update_check_cache()


def test_is_newer_compares_semver_segments() -> None:
    assert _is_newer("1.6.0", "1.5.0") is True
    assert _is_newer("1.5.0", "1.5.0") is False
    assert _is_newer("1.5.1", "1.5.0") is True
    assert _is_newer("2.0.0", "1.9.9") is True
    assert _is_newer("unknown", "1.5.0") is False


@pytest.mark.asyncio
async def test_check_for_updates_reports_available_release() -> None:
    current = get_plumber_version()

    async def fake_fetch() -> str:
        return "99.0.0"

    with patch("src.utils.updater._fetch_latest_pypi_version", new=fake_fetch):
        result = await check_for_updates(force_refresh=True)

    assert result.current_version == current
    assert result.latest_version == "99.0.0"
    assert result.update_available is True
    assert result.pypi_url == "https://pypi.org/project/matryca-logseq/"


@pytest.mark.asyncio
async def test_check_for_updates_falls_back_when_pypi_unreachable() -> None:
    current = get_plumber_version()

    with patch(
        "src.utils.updater._fetch_latest_pypi_version",
        new=AsyncMock(return_value=None),
    ):
        result = await check_for_updates(force_refresh=True)

    assert result.current_version == current
    assert result.latest_version == current
    assert result.update_available is False


@pytest.mark.asyncio
async def test_check_for_updates_uses_cache_until_forced() -> None:
    calls = 0

    async def fake_fetch() -> str:
        nonlocal calls
        calls += 1
        return "99.0.0"

    with patch("src.utils.updater._fetch_latest_pypi_version", new=fake_fetch):
        first = await check_for_updates(force_refresh=True)
        second = await check_for_updates()
        third = await check_for_updates(force_refresh=True)

    assert first.latest_version == "99.0.0"
    assert second.latest_version == "99.0.0"
    assert third.latest_version == "99.0.0"
    assert calls == 2


def test_update_check_to_dict_serializes_result() -> None:
    payload = update_check_to_dict(
        UpdateCheckResult(
            current_version="1.5.0",
            latest_version="1.6.0",
            update_available=True,
            pypi_url="https://pypi.org/project/matryca-logseq/",
        )
    )
    assert payload == {
        "current_version": "1.5.0",
        "latest_version": "1.6.0",
        "update_available": True,
        "pypi_url": "https://pypi.org/project/matryca-logseq/",
    }
