"""Shared helpers for Matryca Plumber cognitive lint modules."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ...graph.markdown_blocks import graph_safe_page_path
from ...graph.path_sandbox import resolved_graph_root

_WIKILINK = re.compile(r"\[\[([^\]#|]+)(?:\|[^\]]+)?\]\]")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_TAG = re.compile(r"(?<![\w/])#([\w/-]+)")
_PROPERTY = re.compile(r"^\s*([a-zA-Z0-9_-]+)::\s*(.*)$")


@dataclass
class ModuleOutcome:
    """Per-module side-effect summary."""

    pages_created: list[str] = field(default_factory=list)
    pages_modified: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def extract_wikilink_targets(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _WIKILINK.finditer(text):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            out.append(target)
    return out


def page_file_exists(graph_root: Path, page_title: str) -> bool:
    try:
        path = graph_safe_page_path(graph_root, page_title)
    except ValueError:
        return False
    return path.is_file()


def resolve_page_path(graph_root: Path, page_title: str) -> Path | None:
    try:
        path = graph_safe_page_path(graph_root, page_title)
    except ValueError:
        return None
    return path


def sanitize_page_title(raw: str, *, max_len: int = 80) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]', "-", raw.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len] if len(cleaned) > max_len else cleaned


def context_around_wikilink(
    content: str,
    link_target: str,
    *,
    radius: int = 3,
) -> str:
    """Return up to ``radius`` blocks above/below the first ``[[target]]`` occurrence."""
    needle = f"[[{link_target}]]"
    lines = content.splitlines()
    anchor = next((i for i, line in enumerate(lines) if needle in line), None)
    if anchor is None:
        return content[:1200]
    bullets = [i for i, line in enumerate(lines) if _BULLET.match(line)]
    if not bullets:
        start = max(0, anchor - radius)
        end = min(len(lines), anchor + radius + 1)
        return "\n".join(lines[start:end])
    block_start = 0
    for idx in bullets:
        if idx <= anchor:
            block_start = idx
        else:
            break
    pos = bullets.index(block_start) if block_start in bullets else 0
    lo = max(0, pos - radius)
    hi = min(len(bullets), pos + radius + 1)
    span_start = bullets[lo]
    span_end = bullets[hi] if hi < len(bullets) else len(lines)
    return "\n".join(lines[span_start:span_end])


def extract_inline_tags(text: str) -> set[str]:
    return {m.group(1).casefold() for m in _TAG.finditer(text)}


def page_property_keys(text: str) -> dict[str, str]:
    keys: dict[str, str] = {}
    for line in text.splitlines():
        match = _PROPERTY.match(line)
        if match:
            keys[match.group(1).casefold()] = match.group(2).strip()
    return keys


def list_existing_page_titles(graph_root: Path) -> set[str]:
    root = resolved_graph_root(graph_root)
    pages_dir = root / "pages"
    if not pages_dir.is_dir():
        return set()
    titles: set[str] = set()
    for path in pages_dir.rglob("*.md"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            titles.add(rel.removeprefix("pages/").removesuffix(".md"))
    return titles


__all__ = [
    "ModuleOutcome",
    "context_around_wikilink",
    "extract_inline_tags",
    "extract_wikilink_targets",
    "list_existing_page_titles",
    "page_file_exists",
    "page_property_keys",
    "resolve_page_path",
    "sanitize_page_title",
]
