"""Split dense block subtrees into dedicated pages (atomic extract + link stub)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from ...graph.generational_cache import patch_generational_caches_for_paths
from ...graph.markdown_blocks import atomic_write_bytes, graph_safe_page_path, locate_block_by_uuid
from ...graph.page_properties import inject_page_property
from ...graph.page_write_lock import page_rmw_lock
from ._shared import ModuleOutcome, sanitize_page_title

_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ID_LINE = re.compile(
    r"^\s*id::\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*$",
    re.IGNORECASE,
)


def _count_subtree_bullets(lines: list[str], bullet_idx: int, end_exclusive: int) -> int:
    count = 0
    for i in range(bullet_idx, end_exclusive):
        if _BULLET.match(lines[i].rstrip("\n")):
            count += 1
    return count


def _subtree_text(lines: list[str], start: int, end: int) -> str:
    chunk = lines[start:end]
    if not chunk:
        return ""
    first = chunk[0].rstrip("\n")
    first_match = _BULLET.match(first)
    base_indent = len(first_match.group(1)) if first_match else 0
    normalized: list[str] = []
    for line in chunk:
        raw = line.rstrip("\n")
        match = _BULLET.match(raw)
        if match:
            indent = max(0, len(match.group(1)) - base_indent)
            normalized.append(" " * indent + "- " + match.group(2))
        else:
            normalized.append(raw)
    return "\n".join(normalized).strip() + "\n"


def run_auto_split(
    graph_root: Path,
    page_path: Path,
    page_title: str,
    *,
    threshold: int,
) -> ModuleOutcome:
    """Extract oversized subtrees into new pages and replace them with wikilink stubs."""
    outcome = ModuleOutcome()
    if not page_path.is_file():
        return outcome

    with page_rmw_lock(page_path):
        text = page_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        stripped = [ln.rstrip("\n") for ln in lines]
        candidates: list[tuple[int, int, int, str]] = []

        for line in stripped:
            id_match = _ID_LINE.match(line)
            if not id_match:
                continue
            located = locate_block_by_uuid(stripped, id_match.group(1))
            if located is None:
                continue
            bullet_idx, _id_idx, end = located
            block_count = _count_subtree_bullets(stripped, bullet_idx, end)
            if block_count < threshold:
                continue
            bullet_match = _BULLET.match(stripped[bullet_idx])
            if not bullet_match:
                continue
            snippet = bullet_match.group(2)[:48]
            candidates.append((bullet_idx, end, block_count, snippet))

        if not candidates:
            return outcome

        ordered = sorted(candidates, key=lambda c: c[0], reverse=True)
        for bullet_idx, end, block_count, snippet in ordered:
            subtree = _subtree_text(lines, bullet_idx, end)
            if not subtree.strip():
                continue
            child_title = sanitize_page_title(f"{page_title} — {snippet}")
            try:
                new_page_path = graph_safe_page_path(graph_root, child_title)
            except ValueError:
                continue
            new_page_path.parent.mkdir(parents=True, exist_ok=True)

            if not new_page_path.is_file():
                header = inject_page_property(
                    f"- Extracted from [[{page_title}]] ({block_count} blocks)\n",
                    "matryca-split",
                    "auto",
                )
                atomic_write_bytes(
                    new_page_path,
                    (header + "\n" + subtree).encode("utf-8"),
                    graph_root=graph_root,
                )
                outcome.pages_created.append(child_title)

            bullet_match = _BULLET.match(lines[bullet_idx].rstrip("\n"))
            indent = bullet_match.group(1) if bullet_match else ""
            new_block_uuid = str(uuid.uuid4())
            replacement = (
                f"{indent}- [[{child_title}]]\n"
                f"{indent}  id:: {new_block_uuid}\n"
                f"{indent}  matryca-split-from:: [[{page_title}]]\n"
            )
            lines[bullet_idx:end] = [replacement]
            outcome.pages_modified.append(page_title)
            outcome.details.append(f"split:{child_title} blocks={block_count}")

        atomic_write_bytes(page_path, "".join(lines).encode("utf-8"), graph_root=graph_root)

    patch_generational_caches_for_paths(graph_root, [page_path])
    for title in outcome.pages_created:
        try:
            created = graph_safe_page_path(graph_root, title)
        except ValueError:
            continue
        if created.is_file():
            patch_generational_caches_for_paths(graph_root, [created])

    return outcome


__all__ = ["run_auto_split"]
