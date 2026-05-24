"""Shared regex validation for graph search and property-line edits."""

from __future__ import annotations

import re

MAX_REGEX_PATTERN_LEN = 256
_CATASTROPHIC_REGEX = re.compile(
    r"(\(\?\:|\(\+\)|\(\*\)|\{,\}|\(\.\+\)\+|\(\.\*\)\+|\(\[[^\]]+\]\)\+|"
    r"\([^)]*[+*][^)]*\)[+*])",
)


def validate_regex_pattern(pattern: str, flags: int = 0) -> re.Pattern[str]:
    """Compile ``pattern`` or raise ``ValueError`` with a short reason."""
    if len(pattern) > MAX_REGEX_PATTERN_LEN:
        msg = f"regex pattern exceeds max length ({MAX_REGEX_PATTERN_LEN})"
        raise ValueError(msg)
    if _CATASTROPHIC_REGEX.search(pattern):
        msg = "regex pattern rejected (catastrophic backtracking risk)"
        raise ValueError(msg)
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        msg = f"Invalid regex: {exc}"
        raise ValueError(msg) from exc


__all__ = ["MAX_REGEX_PATTERN_LEN", "validate_regex_pattern"]
