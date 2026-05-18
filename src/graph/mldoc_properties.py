"""Logseq ``key:: value`` property lines — structural rules aligned with Logseq/mldoc invariants.

Pure Python: no Rust/Clojure runtime. These helpers encode stable *line-level* rules used by
Logseq's Markdown outliner so callers avoid regex false positives (bullets, URLs) and can
tokenize values without splitting inside quotes or ``[[wikilinks]]``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BULLET = re.compile(r"^\s*[-*+]\s+")
# ``#``-first lines are treated as headings/comments, not block properties.
_HASH_HEAD = re.compile(r"^\s*#(?:\s|$|#)")


@dataclass(frozen=True, slots=True)
class ParsedPropertyLine:
    """One logical ``key:: value`` line (excluding list bullets)."""

    indent: str
    key_raw: str
    key_normalized: str
    sep_after_colons: str
    value_raw: str
    value_start: int

    @property
    def line_core(self) -> str:
        """Full line without trailing newline semantics (caller strips)."""
        return f"{self.indent}{self.key_raw}::{self.sep_after_colons}{self.value_raw}"


def normalize_logseq_property_key(key: str) -> str:
    """Case-insensitive key form (Unicode casefold)."""
    return key.strip().casefold()


def parse_logseq_property_line(line: str) -> ParsedPropertyLine | None:
    """Parse a single *stripped* logical line into ``key:: value`` or return ``None``.

    * Not a Markdown bullet (``-`` / ``*`` / ``+``).
    * Not a ``#`` heading / comment-first line.
    * Uses the **first** ``::`` pair only so values may contain ``::`` inside ``[[links]]``.
    """
    s = line.rstrip("\n\r")
    if not s.strip() or "::" not in s:
        return None
    if _BULLET.match(s):
        return None
    if _HASH_HEAD.match(s):
        return None
    idx = s.index("::")
    left, right = s[:idx], s[idx + 2 :]
    m_indent = re.match(r"^(\s*)(\S.*)$", left)
    if not m_indent:
        return None
    indent, key_raw = m_indent.group(1), m_indent.group(2).rstrip()
    if not key_raw or key_raw.startswith("#"):
        return None
    if ":" in key_raw:
        return None
    sp_m = re.match(r"^(\s*)(.*)$", right, re.DOTALL)
    if not sp_m:
        return None
    sep_after, value_raw = sp_m.group(1), sp_m.group(2)
    value_start = idx + 2 + len(sep_after)
    kn = normalize_logseq_property_key(key_raw)
    if not kn:
        return None
    return ParsedPropertyLine(
        indent=indent,
        key_raw=key_raw,
        key_normalized=kn,
        sep_after_colons=sep_after,
        value_raw=value_raw,
        value_start=value_start,
    )


def is_logseq_block_property_line(stripped_line: str) -> bool:
    """True if *stripped_line* is a Logseq block property (not a bullet, not a # heading)."""
    return parse_logseq_property_line(stripped_line) is not None


def split_logseq_property_list_values(raw_value: str) -> list[str]:
    """Split comma-separated property values without breaking quotes or ``[[...]]``.

    Commas inside double-quoted segments or wikilinks do not split. Backslash escapes the
    next character inside double quotes only.
    """
    v = raw_value.strip()
    if not v:
        return []
    out: list[str] = []
    cur: list[str] = []
    i = 0
    in_dq = False
    link_depth = 0
    while i < len(v):
        ch = v[i]
        if in_dq:
            if ch == "\\" and i + 1 < len(v):
                cur.append(ch)
                cur.append(v[i + 1])
                i += 2
                continue
            if ch == '"':
                in_dq = False
            cur.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dq = True
            cur.append(ch)
            i += 1
            continue
        if ch == "[" and v[i : i + 2] == "[[":
            link_depth += 1
            cur.extend("[[")
            i += 2
            continue
        if link_depth > 0 and ch == "]" and v[i : i + 2] == "]]":
            link_depth -= 1
            cur.extend("]]")
            i += 2
            continue
        if ch == "," and link_depth == 0:
            piece = "".join(cur).strip()
            if piece:
                out.append(piece)
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def double_quoted_spans_in_value(value_raw: str, *, base_offset: int) -> list[tuple[int, int]]:
    """Char intervals (start inclusive, end exclusive) of ``"..."`` inside *value_raw*.

    *base_offset* shifts indices to absolute positions in the full file line.
    """
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(value_raw):
        if value_raw[i] != '"':
            i += 1
            continue
        start = i
        i += 1
        while i < len(value_raw):
            if value_raw[i] == "\\":
                i += 2
                continue
            if value_raw[i] == '"':
                spans.append((base_offset + start, base_offset + i + 1))
                i += 1
                break
            i += 1
        else:
            break
    return spans


__all__ = [
    "ParsedPropertyLine",
    "double_quoted_spans_in_value",
    "is_logseq_block_property_line",
    "normalize_logseq_property_key",
    "parse_logseq_property_line",
    "split_logseq_property_list_values",
]
