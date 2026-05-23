"""Prepare reduced LLM payloads for giant pages (Phase 1 summaries + semantic skeleton)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from ..graph.master_catalog import (
    CatalogEntry,
    extract_catalog_fields_from_content,
    load_master_catalog,
)
from .plumber_config import PlumberLintConfig

PayloadSource = Literal["raw", "summary", "skeleton", "truncated"]

MAX_PAYLOAD_CHARS = 25_000
_PAYLOAD_TRUNCATION_NOTICE = (
    "\n\n[TRUNCATED: File exceeded 25k characters. To protect the LLM context window, "
    "the remainder of this file was omitted.]"
)

_WIKILINK = re.compile(r"\[\[[^\]#|]+(?:\|[^\]]+)?\]\]")
_TAG = re.compile(r"(?<![\w/])#[\w/-]+")
_PROPERTY = re.compile(r"^\s*[a-zA-Z0-9_-]+::\s*(.*)$")


def cap_llm_payload_chars(content: str, *, max_chars: int = MAX_PAYLOAD_CHARS) -> str:
    """Hard-cap text destined for local LLM context windows."""
    if len(content) <= max_chars:
        return content
    budget = max_chars - len(_PAYLOAD_TRUNCATION_NOTICE)
    if budget <= 0:
        return _PAYLOAD_TRUNCATION_NOTICE[:max_chars]
    return content[:budget] + _PAYLOAD_TRUNCATION_NOTICE


def extract_semantic_skeleton(content: str) -> str:
    """Keep only lines with wikilinks, inline tags, or ``key:: value`` properties."""
    kept: list[str] = []
    for line in content.splitlines():
        if _WIKILINK.search(line) or _TAG.search(line) or _PROPERTY.match(line):
            kept.append(line)
    return "\n".join(kept)


def _format_summary_payload(entry: CatalogEntry) -> str:
    lines = [entry.summary.strip()]
    if entry.domain:
        lines.append(f"domain:: {entry.domain}")
    if entry.tags:
        lines.append("suggested-tags:: " + " ".join(f"#{tag.lstrip('#')}" for tag in entry.tags))
    return "\n".join(lines)


def _resolve_phase1_summary(
    graph_root: Path,
    page_title: str,
    content: str,
) -> str | None:
    catalog = load_master_catalog(graph_root)
    resolved = catalog.get_case_insensitive(page_title)
    if resolved is not None:
        _, entry = resolved
        if entry.summary.strip():
            return _format_summary_payload(entry)

    extracted = extract_catalog_fields_from_content(content)
    if extracted is not None and extracted.summary.strip():
        return _format_summary_payload(extracted)
    return None


def prepare_llm_context_payload(
    graph_root: Path,
    page_title: str,
    content: str,
    *,
    config: PlumberLintConfig,
) -> tuple[str, PayloadSource]:
    """Return LLM-safe page text, preferring Phase 1 summaries for giant files."""
    if len(content) <= config.mapreduce_trigger_chars:
        return cap_llm_payload_chars(content), "raw"

    summary = _resolve_phase1_summary(graph_root, page_title, content)
    if summary:
        return cap_llm_payload_chars(summary), "summary"

    skeleton = extract_semantic_skeleton(content)
    if skeleton.strip():
        return cap_llm_payload_chars(skeleton), "skeleton"

    truncated = content[: config.mapreduce_trigger_chars]
    return cap_llm_payload_chars(truncated), "truncated"


__all__ = [
    "MAX_PAYLOAD_CHARS",
    "PayloadSource",
    "cap_llm_payload_chars",
    "extract_semantic_skeleton",
    "prepare_llm_context_payload",
]
