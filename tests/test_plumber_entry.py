"""Tests for the ``matryca-plumber`` console-script router."""

from __future__ import annotations

import pytest
from src.plumber_entry import _normalize_cli_argv


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], None),
        (["status"], ["plumber", "status"]),
        (["start", "--foreground"], ["plumber", "start", "--foreground"]),
        (["plumber", "status"], ["plumber", "status"]),
        (["read", "page", "Home"], ["read", "page", "Home"]),
        (["--help"], ["--help"]),
        (["-h"], ["-h"]),
        (["unknown"], None),
    ],
)
def test_normalize_cli_argv(argv: list[str], expected: list[str] | None) -> None:
    assert _normalize_cli_argv(argv) == expected
