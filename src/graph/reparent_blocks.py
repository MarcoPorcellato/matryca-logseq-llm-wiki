"""Reparent flat Logseq bullets under new category headings (indent-only moves)."""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .global_fence_scanner import compute_page_protected_line_indices
from .markdown_blocks import (
    atomic_write_bytes,
    bullet_indent_unit,
    locate_block_by_uuid,
    read_page_lines,
)
from .page_write_lock import page_rmw_lock

_BULLET = re.compile(r"^(\s*)[-*+]\s+")


def _leading_ws_len(line: str) -> tuple[str, int]:
    m = re.match(r"^([ \t]*)", line)
    if not m:
        return "", 0
    ws = m.group(1)
    return ws, len(ws)


def _shift_block_lines(subtree: list[str], delta_spaces: int) -> list[str]:
    if delta_spaces == 0:
        return list(subtree)
    out: list[str] = []
    for ln in subtree:
        core = ln.rstrip("\n")
        nl = ln[len(core) :]
        if not core.strip():
            out.append(ln)
            continue
        ws, wlen = _leading_ws_len(core)
        if delta_spaces > 0:
            out.append(" " * delta_spaces + core + nl)
        else:
            strip = min(-delta_spaces, len(ws))
            new_core = ws[strip:] + core[wlen:]
            out.append(new_core + nl)
    return out


@dataclass(frozen=True, slots=True)
class ReparentResult:
    ok: bool
    code: str
    hint: str
    dry_run: bool
    path: str | None
    preview_lines: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "code": self.code,
            "hint": self.hint,
            "dry_run": self.dry_run,
            "path": self.path,
            "preview_lines": self.preview_lines,
        }


def refactor_logseq_blocks(
    graph_root: str | Path,
    page_ref: str,
    groups: list[dict[str, object]],
    *,
    dry_run: bool = True,
) -> ReparentResult:
    """Insert category bullets and nest member subtrees under them (same indent class).

    ``groups`` items: ``{"category": str, "block_uuids": list[str]}``.
    """
    path, lines, err = read_page_lines(graph_root, page_ref)
    if err or path is None or lines is None:
        return ReparentResult(
            ok=False,
            code=err or "read_error",
            hint="Could not read page.",
            dry_run=dry_run,
            path=None,
            preview_lines=[],
        )

    with page_rmw_lock(path):
        stripped = [ln.rstrip("\n") for ln in lines]
        protected = compute_page_protected_line_indices("".join(lines))
        all_uuids: list[str] = []
        parsed_groups: list[tuple[str, list[str]]] = []
        for g in groups:
            if not isinstance(g, dict):
                return ReparentResult(
                    ok=False,
                    code="bad_group",
                    hint="Each group must be an object with `category` and `block_uuids`.",
                    dry_run=dry_run,
                    path=str(path),
                    preview_lines=[],
                )
            cat = str(g.get("category", "")).strip()
            raw_uuids = g.get("block_uuids")
            if not cat or not isinstance(raw_uuids, list):
                return ReparentResult(
                    ok=False,
                    code="bad_group",
                    hint="Each group needs non-empty `category` and `block_uuids` list.",
                    dry_run=dry_run,
                    path=str(path),
                    preview_lines=[],
                )
            uuids = [str(u).strip() for u in raw_uuids if str(u).strip()]
            if not uuids:
                return ReparentResult(
                    ok=False,
                    code="empty_group",
                    hint="block_uuids must not be empty.",
                    dry_run=dry_run,
                    path=str(path),
                    preview_lines=[],
                )
            parsed_groups.append((cat, uuids))
            all_uuids.extend(uuids)

        if len(set(all_uuids)) != len(all_uuids):
            return ReparentResult(
                ok=False,
                code="duplicate_uuid",
                hint="A block UUID appears in more than one group.",
                dry_run=dry_run,
                path=str(path),
                preview_lines=[],
            )

        locs: dict[str, tuple[int, int, int]] = {}
        base_indents: list[int] = []
        for u in all_uuids:
            loc = locate_block_by_uuid(stripped, u)
            if loc is None:
                return ReparentResult(
                    ok=False,
                    code="uuid_not_found",
                    hint=f"No `id::` match for `{u}`.",
                    dry_run=dry_run,
                    path=str(path),
                    preview_lines=[],
                )
            b, _i, e = loc
            if set(range(b, e)) & protected:
                return ReparentResult(
                    ok=False,
                    code="protected_fence",
                    hint="A selected block overlaps a fenced / query / HTML dead zone.",
                    dry_run=dry_run,
                    path=str(path),
                    preview_lines=[],
                )
            locs[u] = loc
            bm = _BULLET.match(stripped[b])
            base_indents.append(len(bm.group(1)) if bm else 0)

        intervals = sorted((b, e) for b, _i, e in locs.values())
        for i in range(1, len(intervals)):
            prev_b, prev_e = intervals[i - 1]
            b, e = intervals[i]
            if b < prev_e:
                return ReparentResult(
                    ok=False,
                    code="overlapping_blocks",
                    hint=(
                        "Selected blocks overlap in the file; pick disjoint blocks "
                        "or a narrower refactor scope."
                    ),
                    dry_run=dry_run,
                    path=str(path),
                    preview_lines=[],
                )

        if base_indents and max(base_indents) != min(base_indents):
            return ReparentResult(
                ok=False,
                code="indent_mismatch",
                hint="All member blocks must share the same list indent (flat siblings).",
                dry_run=dry_run,
                path=str(path),
                preview_lines=[],
            )

        first_start = min(b for b, _i, _e in locs.values())

        kept: list[str] = []
        cursor = 0
        for b, e in intervals:
            kept.extend(lines[cursor:b])
            cursor = e
        kept.extend(lines[cursor:])

        unit = bullet_indent_unit(lines, intervals[0][0]) if intervals else "  "
        base_ws_len = base_indents[0] if base_indents else 0
        base_ws = " " * base_ws_len
        child_ws = base_ws + unit

        insert_chunks: list[str] = []
        previews: list[str] = []
        for cat, uuids in parsed_groups:
            cid = str(uuid.uuid4())
            insert_chunks.append(f"{base_ws}- {cat}\n")
            insert_chunks.append(f"{child_ws}id:: {cid}\n")
            previews.append(f"- {cat}")
            for u in uuids:
                b, _i, e = locs[u]
                subtree = lines[b:e]
                bm = _BULLET.match(stripped[b])
                old_len = len(bm.group(1)) if bm else 0
                delta = len(child_ws) - old_len
                shifted = _shift_block_lines(subtree, delta)
                insert_chunks.extend(shifted)

        insert_blob = "".join(insert_chunks)
        new_lines = kept[:first_start] + insert_blob.splitlines(keepends=True) + kept[first_start:]
        new_text = "".join(new_lines)

        if dry_run:
            return ReparentResult(
                ok=True,
                code="dry_run_ok",
                hint="Re-run with dry_run=false after a git snapshot to apply.",
                dry_run=True,
                path=str(path),
                preview_lines=previews[:20],
            )

        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        atomic_write_bytes(path, new_text.encode("utf-8"))

        return ReparentResult(
            ok=True,
            code="applied",
            hint=f"Backup `{bak.name}`; reparented {len(all_uuids)} blocks.",
            dry_run=False,
            path=str(path),
            preview_lines=previews[:20],
        )


__all__ = ["ReparentResult", "refactor_logseq_blocks"]
