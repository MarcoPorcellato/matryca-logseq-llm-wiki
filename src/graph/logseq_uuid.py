"""Logseq block UUID shape checks (v4/v5) and pre-flight ``((uuid))`` validation."""

from __future__ import annotations

import re
import uuid

# Canonical 36-char hyphenated UUID (Logseq block refs and ``id::`` values).
UUID_CANONICAL_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
)

# Logseq block references: ((...)) — capture inner token (may be malformed).
_BLOCK_REF_INNER_RE = re.compile(r"\(\(([^)]+)\)\)")

_BLOCK_REF_PREFLIGHT_ERROR = (
    "Error: Malformed UUID detected in block ref. "
    "Must be standard 36-char format. Please fix the typo and retry"
)

MALFORMED_BLOCK_REF_MARKER = _BLOCK_REF_PREFLIGHT_ERROR


def is_standard_uuid_shape(value: str) -> bool:
    """True when ``value`` is exactly 36 chars and parses as a UUID."""
    if len(value) != 36 or not UUID_CANONICAL_RE.match(value):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def is_logseq_block_uuid(value: str) -> bool:
    """True for UUID v4 or v5 (Logseq accepts both for block ``id::`` / refs)."""
    if not is_standard_uuid_shape(value):
        return False
    parsed = uuid.UUID(value)
    return parsed.version in (4, 5)


def find_malformed_block_refs(
    text: str,
    *,
    protected_lines: set[int] | None = None,
) -> list[str]:
    """Return inner ``((...))`` tokens that are not valid 36-char UUIDs.

    When ``protected_lines`` is set, matches on those 0-based line indices are
    ignored (fenced code, HTML comments, advanced queries).
    """
    bad: list[str] = []
    protected = protected_lines or set()
    for line_idx, line in enumerate(text.splitlines()):
        if line_idx in protected:
            continue
        for match in _BLOCK_REF_INNER_RE.finditer(line):
            inner = match.group(1).strip()
            if not is_standard_uuid_shape(inner):
                bad.append(inner)
    return bad


def assert_valid_block_refs_in_markdown(text: str) -> None:
    """Reject markdown that contains malformed ``((uuid))`` block references."""
    from .global_fence_scanner import compute_page_protected_line_indices

    protected = compute_page_protected_line_indices(text)
    bad = find_malformed_block_refs(text, protected_lines=protected)
    if bad:
        sample = bad[0]
        if len(sample) > 48:
            sample = f"{sample[:45]}..."
        msg = f"{_BLOCK_REF_PREFLIGHT_ERROR} (example: (({sample})))."
        raise ValueError(msg)


def is_malformed_block_ref_error(exc: BaseException) -> bool:
    """True when ``exc`` is the atomic-write preflight rejection for bad ``((uuid))`` tokens."""
    return isinstance(exc, ValueError) and MALFORMED_BLOCK_REF_MARKER in str(exc)


__all__ = [
    "MALFORMED_BLOCK_REF_MARKER",
    "UUID_CANONICAL_RE",
    "assert_valid_block_refs_in_markdown",
    "find_malformed_block_refs",
    "is_logseq_block_uuid",
    "is_malformed_block_ref_error",
    "is_standard_uuid_shape",
]
