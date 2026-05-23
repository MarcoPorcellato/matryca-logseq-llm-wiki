"""Inject Logseq **page** properties as true frontmatter at the top of a markdown page."""

from __future__ import annotations

import functools
import importlib.metadata

from .mldoc_properties import (
    normalize_logseq_property_key,
    parse_logseq_property_line,
    split_logseq_property_list_values,
)

_MERGE_LIST_KEYS = frozenset({"alias", "aliases", "tags"})
_RAW_FRONTMATTER_LIST_KEYS = frozenset({"alias", "aliases", "tags"})
PLUMBER_CREATED_BY_KEY = "created-by"
PLUMBER_CREATED_BY_VALUE = "plumber"
PLUMBER_MADE_BY_KEY = "made-by"
PLUMBER_MADE_BY_PREFIX = "matryca plumber"
_PACKAGE_NAME = "matryca-logseq"


@functools.lru_cache(maxsize=1)
def get_plumber_version() -> str:
    """Return the installed Matryca Plumber package version from ``pyproject.toml`` metadata."""
    try:
        return importlib.metadata.version(_PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _plumber_made_by_value() -> str:
    return f"{PLUMBER_MADE_BY_PREFIX} v{get_plumber_version()}"


def _is_legacy_plumber_authored(properties: dict[str, str]) -> bool:
    return properties.get(PLUMBER_CREATED_BY_KEY, "").casefold() == PLUMBER_CREATED_BY_VALUE


def _is_versioned_plumber_authored(properties: dict[str, str]) -> bool:
    made_by = properties.get(PLUMBER_MADE_BY_KEY, "").casefold()
    return made_by.startswith(PLUMBER_MADE_BY_PREFIX.casefold())


def _strip_frontmatter_list_token(raw: str) -> str:
    """Strip inline Logseq markup from a frontmatter list token (``#tag``, ``[[link]]``)."""
    token = raw.strip()
    if token.startswith("[[") and token.endswith("]]"):
        token = token[2:-2].strip()
    token = token.lstrip("#").strip()
    return token


def _sanitize_frontmatter_list_value(norm_key: str, value: str) -> str:
    if norm_key not in _RAW_FRONTMATTER_LIST_KEYS:
        return value.strip()
    items = split_logseq_property_list_values(value.strip())
    cleaned = [_strip_frontmatter_list_token(item) for item in items]
    cleaned = [item for item in cleaned if item]
    return ", ".join(cleaned)


def _strip_lines(lines: list[str]) -> list[str]:
    return [ln.rstrip("\n") for ln in lines]


def _frontmatter_span(stripped: list[str]) -> tuple[int, int]:
    """Return ``(start, end_exclusive)`` for top-of-file page property lines."""
    if not stripped:
        return 0, 0
    start = 0
    while start < len(stripped) and not stripped[start].strip():
        start += 1
    end = start
    for i in range(start, len(stripped)):
        line = stripped[i]
        if not line.strip():
            break
        if parse_logseq_property_line(line):
            end = i + 1
            continue
        break
    return start, end


def _page_property_keys_in_frontmatter(stripped: list[str]) -> dict[str, str]:
    start, end = _frontmatter_span(stripped)
    keys: dict[str, str] = {}
    for i in range(start, end):
        parsed = parse_logseq_property_line(stripped[i])
        if parsed:
            keys[parsed.key_normalized] = parsed.value_raw.strip()
    return keys


def _find_frontmatter_key_line(
    stripped: list[str],
    fm_start: int,
    fm_end: int,
    norm_key: str,
) -> int | None:
    for i in range(fm_start, fm_end):
        parsed = parse_logseq_property_line(stripped[i])
        if parsed and parsed.key_normalized == norm_key:
            return i
    return None


def _merge_list_property_value(existing_value: str, new_value: str) -> str | None:
    """Append comma-separated values when absent (case-insensitive token match)."""
    existing_items = split_logseq_property_list_values(existing_value)
    new_items = split_logseq_property_list_values(new_value)
    existing_folds = {item.casefold() for item in existing_items}
    additions = [item for item in new_items if item.casefold() not in existing_folds]
    if not additions:
        return None
    return ", ".join([*existing_items, *additions])


def _content_start(stripped: list[str], fm_end: int) -> int:
    """Index of the first non-blank line after frontmatter (or 0 when none)."""
    idx = fm_end
    while idx < len(stripped) and not stripped[idx].strip():
        idx += 1
    return idx


def inject_page_property(markdown_text: str, key: str, value: str) -> str:
    """Insert or merge ``key:: value`` as page-level frontmatter at the top of the file."""
    norm_key = normalize_logseq_property_key(key)
    if not norm_key or not value.strip():
        return markdown_text
    if markdown_text and not markdown_text.strip():
        return markdown_text
    value = _sanitize_frontmatter_list_value(norm_key, value.strip())

    lines = markdown_text.splitlines(keepends=True)
    if not lines:
        lines = [""]
    stripped = _strip_lines(lines)
    existing = _page_property_keys_in_frontmatter(stripped)

    if norm_key in existing:
        if norm_key not in _MERGE_LIST_KEYS:
            return markdown_text
        fm_start, fm_end = _frontmatter_span(stripped)
        line_idx = _find_frontmatter_key_line(stripped, fm_start, fm_end, norm_key)
        if line_idx is None:
            return markdown_text
        merged = _merge_list_property_value(existing[norm_key], value)
        if merged is None:
            return markdown_text
        parsed = parse_logseq_property_line(stripped[line_idx])
        if parsed is None:
            return markdown_text
        newline = "\n" if lines[line_idx].endswith("\n") else ""
        lines[line_idx] = f"{parsed.key_raw}::{parsed.sep_after_colons}{merged}{newline}"
        return "".join(lines)

    fm_start, fm_end = _frontmatter_span(stripped)
    prop_line = f"{key}:: {value}\n"

    if fm_end > fm_start:
        insert_at = fm_end
        lines.insert(insert_at, prop_line)
        return "".join(lines)

    content_start = _content_start(stripped, fm_end)
    if content_start < len(stripped):
        lines.insert(content_start, "\n")
        lines.insert(content_start, prop_line)
        return "".join(lines)

    if markdown_text.strip():
        return prop_line + "\n" + markdown_text
    return prop_line


def inject_page_properties(markdown_text: str, properties: dict[str, str]) -> str:
    """Insert multiple page properties idempotently."""
    text = markdown_text
    for key, value in sorted(properties.items()):
        if value.strip():
            text = inject_page_property(text, key, value.strip())
    return text


def is_plumber_authored_page(markdown_text: str) -> bool:
    """True when frontmatter marks the page as spawned by the Plumber agent."""
    properties = page_property_keys(markdown_text)
    return _is_versioned_plumber_authored(properties) or _is_legacy_plumber_authored(properties)


def stamp_plumber_authored_page(markdown_text: str) -> str:
    """Inject ``made-by:: matryca plumber v{version}`` at the top of a newly created page."""
    if is_plumber_authored_page(markdown_text):
        return markdown_text
    return inject_page_property(markdown_text, PLUMBER_MADE_BY_KEY, _plumber_made_by_value())


def page_property_keys(markdown_text: str) -> dict[str, str]:
    """Return ``key -> value`` for page-level frontmatter properties."""
    stripped = [ln.rstrip("\n") for ln in markdown_text.splitlines()]
    return _page_property_keys_in_frontmatter(stripped)


__all__ = [
    "PLUMBER_CREATED_BY_KEY",
    "PLUMBER_CREATED_BY_VALUE",
    "PLUMBER_MADE_BY_KEY",
    "PLUMBER_MADE_BY_PREFIX",
    "get_plumber_version",
    "inject_page_properties",
    "inject_page_property",
    "is_plumber_authored_page",
    "page_property_keys",
    "stamp_plumber_authored_page",
]
