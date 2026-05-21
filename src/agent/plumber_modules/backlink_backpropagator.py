"""Contextual backlink backpropagation for auto_wikilink semantic corrections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ...graph.alias_index import AliasIndex, resolve_canonical_page_title
from ...graph.generational_cache import patch_generational_caches_for_paths
from ...graph.markdown_blocks import atomic_write_bytes
from ...graph.page_write_lock import page_rmw_lock
from ._shared import ModuleOutcome, page_file_exists, resolve_page_path

_BACKLINK_HEADER = "### Matryca Backlink Context"
_WIKILINK = re.compile(r"\[\[([^\]#|]+)(?:\|[^\]]+)?\]\]")
_REF_FROM = re.compile(r"^\s*(?:-\s+)?referenced-from::\s*\[\[([^\]]+)\]\]\s*$")
_SOURCE_BLOCK = re.compile(r"^\s*(?:-\s+)?source-block-uuid::\s*(\S+)\s*$")
_CONTEXT_SUMMARY = re.compile(r"^\s*(?:-\s+)?context-summary::\s*(.*)$")
LintType = Literal["auto_wikilink", "tag_hygiene", "anomaly_warning"]


@dataclass
class BacklinkCorrection:
    """Minimal correction record for backprop (avoids circular imports)."""

    block_uuid: str
    original_text: str
    corrected_text: str
    lint_type: LintType
    reason: str


def _new_wikilinks(original: str, corrected: str, *, alias_index: AliasIndex | None) -> list[str]:
    orig = {m.group(1).strip() for m in _WIKILINK.finditer(original)}
    added: list[str] = []
    for match in _WIKILINK.finditer(corrected):
        target = match.group(1).strip()
        if not target or target in orig:
            continue
        if alias_index is not None:
            target = resolve_canonical_page_title(alias_index, target)
        if target not in orig and target not in added:
            added.append(target)
    return added


def _summary_from_correction(correction: BacklinkCorrection, source_title: str) -> str:
    if correction.reason.strip():
        return correction.reason.strip()
    return (
        f"[[{source_title}]] mentions this concept via auto_wikilink "
        f"({correction.original_text[:80]} → {correction.corrected_text[:80]})"
    )


def _format_backlink_section(
    source_title: str,
    target_title: str,
    summary: str,
    block_uuid: str,
) -> str:
    return (
        f"\n{_BACKLINK_HEADER}\n"
        f"- referenced-from:: [[{source_title}]]\n"
        f"- referenced-to:: [[{target_title}]]\n"
        f"- source-block-uuid:: {block_uuid}\n"
        f"- context-summary:: {summary.strip()}\n"
        f"- matryca-backprop:: true\n"
    )


def _line_body(line: str) -> str:
    return line.rstrip("\n")


def _section_matches(
    section_lines: list[str],
    *,
    source_title: str,
    block_uuid: str,
) -> bool:
    source_ok = False
    uuid_ok = False
    for line in section_lines:
        body = _line_body(line)
        from_match = _REF_FROM.match(body)
        if from_match and from_match.group(1).strip() == source_title:
            source_ok = True
        uuid_match = _SOURCE_BLOCK.match(body)
        if uuid_match and uuid_match.group(1).strip() == block_uuid:
            uuid_ok = True
        if source_ok and uuid_ok:
            return True
    # Legacy blocks without source-block-uuid: match on source title alone.
    return source_ok and not any(_SOURCE_BLOCK.match(_line_body(line)) for line in section_lines)


def _update_section_summary(section_lines: list[str], summary: str) -> list[str]:
    updated: list[str] = []
    replaced = False
    for line in section_lines:
        if _CONTEXT_SUMMARY.match(_line_body(line)):
            updated.append(f"- context-summary:: {summary.strip()}\n")
            replaced = True
        else:
            updated.append(line if line.endswith("\n") else f"{line}\n")
    if not replaced:
        updated.append(f"- context-summary:: {summary.strip()}\n")
    return updated


def _upsert_backlink_in_content(
    content: str,
    *,
    source_title: str,
    target_title: str,
    summary: str,
    block_uuid: str,
) -> tuple[str, bool]:
    """Insert, update, or skip a backlink section (idempotent per source+block UUID)."""
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    idx = 0
    modified = False

    while idx < len(lines):
        line = lines[idx]
        if line.rstrip("\n") != _BACKLINK_HEADER:
            output.append(line)
            idx += 1
            continue

        section_start = len(output)
        output.append(line)
        idx += 1
        section_body: list[str] = []
        while idx < len(lines):
            peek = lines[idx]
            if peek.rstrip("\n").startswith("### ") and peek.rstrip("\n") != _BACKLINK_HEADER:
                break
            section_body.append(peek)
            output.append(peek)
            idx += 1

        if _section_matches(section_body, source_title=source_title, block_uuid=block_uuid):
            existing_summary = ""
            for body_line in section_body:
                match = _CONTEXT_SUMMARY.match(_line_body(body_line))
                if match:
                    existing_summary = match.group(1).strip()
                    break
            if existing_summary == summary.strip():
                continue
            refreshed = _update_section_summary(section_body, summary)
            output[section_start + 1 : section_start + 1 + len(section_body)] = refreshed
            modified = True
            continue

    if modified:
        return "".join(output), True

    ref_from = f"referenced-from:: [[{source_title}]]"
    ref_uuid = f"source-block-uuid:: {block_uuid}"
    if ref_from in content and ref_uuid in content:
        return content, False

    return content.rstrip("\n") + _format_backlink_section(
        source_title,
        target_title,
        summary,
        block_uuid,
    ), True


def run_backlink_backpropagator(
    graph_root: Path,
    source_page: Path,
    source_title: str,
    corrections: list[BacklinkCorrection],
    applied_details: list[str],
    *,
    alias_index: AliasIndex | None = None,
) -> ModuleOutcome:
    """Append inverse context blocks on target pages for applied auto_wikilinks.

    Lock ordering: callers must release the source page lock before invoking this
    function. Each target page is locked independently (never nested with the source
    transaction) so cross-page backprop cannot deadlock with the semantic lint pass.
    """
    _ = source_page
    outcome = ModuleOutcome()
    applied_uuids = {
        detail.split(":", 2)[1] for detail in applied_details if detail.startswith("auto_wikilink:")
    }
    if not applied_uuids:
        return outcome

    for correction in corrections:
        if correction.lint_type != "auto_wikilink":
            continue
        if correction.block_uuid not in applied_uuids:
            continue
        for target in _new_wikilinks(
            correction.original_text,
            correction.corrected_text,
            alias_index=alias_index,
        ):
            if target == source_title:
                continue
            if not page_file_exists(graph_root, target):
                continue
            target_path = resolve_page_path(graph_root, target)
            if target_path is None:
                continue
            summary = _summary_from_correction(correction, source_title)
            modified = False
            with page_rmw_lock(target_path):
                prev = target_path.read_text(encoding="utf-8", errors="replace")
                new_text, changed = _upsert_backlink_in_content(
                    prev,
                    source_title=source_title,
                    target_title=target,
                    summary=summary,
                    block_uuid=correction.block_uuid,
                )
                if not changed:
                    continue
                atomic_write_bytes(target_path, new_text.encode("utf-8"), graph_root=graph_root)
                modified = True
            if modified:
                outcome.pages_modified.append(target)
                outcome.details.append(f"backprop:{source_title}->{target}")
                patch_generational_caches_for_paths(graph_root, [target_path])

    return outcome


__all__ = ["BacklinkCorrection", "run_backlink_backpropagator"]
