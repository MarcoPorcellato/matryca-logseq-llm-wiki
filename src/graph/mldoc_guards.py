"""Protected structural regions inspired by Logseq/mldoc block atomicity (drawers, fences, macros).

Used to skip destructive transforms (e.g. naive sentence splits) when a bullet embeds
indivisible nodes such as fenced code or Org-style drawers like ``:LOGBOOK:``.
"""

from __future__ import annotations

import re

_DRAWER = re.compile(r":(?![\d])[A-Za-z][A-Za-z0-9_-]*:")
_MACRO_OPEN = re.compile(r"\{\{")


def bullet_first_line_refactor_blocked(body: str) -> bool:
    """Return True if *body* (first line of a list bullet) should not be sentence-split.

    Heuristics: fenced code marker, common Logseq drawer markers, ``{{`` macros.
    """
    if "```" in body:
        return True
    if _MACRO_OPEN.search(body):
        return True
    if _DRAWER.search(body):
        return True
    return False


def pre_id_block_lines_protected(lines: list[str], bullet_idx: int, id_line_idx: int) -> bool:
    """True if lines between the bullet and ``id::`` embed drawers, macros, or fenced code."""
    if id_line_idx <= bullet_idx + 1:
        return False
    mid = "".join(lines[bullet_idx + 1 : id_line_idx])
    if "```" in mid:
        return True
    for i in range(bullet_idx + 1, id_line_idx):
        s = lines[i].rstrip("\n")
        if _DRAWER.search(s) or _MACRO_OPEN.search(s):
            return True
    return False


def block_span_has_code_fence_or_drawer(
    lines: list[str],
    *,
    bullet_idx: int,
    id_line_idx: int,
    subtree_end: int,
) -> bool:
    """True if the block subtree (bullet .. ``subtree_end``) embeds drawers, macros, or fences."""
    hi = min(subtree_end, len(lines))
    if hi <= bullet_idx:
        return False
    chunk = "".join(lines[bullet_idx:hi])
    if "```" in chunk:
        return True
    for i in range(bullet_idx, hi):
        s = lines[i].rstrip("\n")
        if _DRAWER.search(s) or _MACRO_OPEN.search(s):
            return True
    return False


__all__ = [
    "block_span_has_code_fence_or_drawer",
    "bullet_first_line_refactor_blocked",
    "pre_id_block_lines_protected",
]
