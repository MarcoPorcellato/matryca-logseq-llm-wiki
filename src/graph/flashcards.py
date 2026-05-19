"""Generate Logseq native ``#card`` flashcards from dense Markdown (SRS-style ``::`` pairs)."""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .markdown_blocks import (
    atomic_write_bytes,
    bullet_indent_unit,
    locate_block_by_uuid,
    read_page_lines,
)
from .mldoc_properties import is_logseq_block_property_line
from .page_write_lock import page_rmw_lock

# Obsidian-style basic card: "question :: answer" in bullet body (not Logseq ``key::`` lines).
_SRS_PAIR = re.compile(
    r"^(?P<indent>\s*)[-*+]\s+(?P<body>.+)$",
)


def _parse_srs_pair_from_bullet(stripped: str) -> tuple[str, str] | None:
    m = _SRS_PAIR.match(stripped)
    if not m:
        return None
    body = m.group("body").strip()
    if is_logseq_block_property_line(stripped):
        return None
    sep = " :: "
    if sep not in body:
        return None
    q, a = body.split(sep, 1)
    q, a = q.strip(), a.strip()
    if not q or not a or len(q) < 2:
        return None
    if q.endswith("::"):
        return None
    return q, a


def _extract_pairs_from_block_text(chunk_lines: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in chunk_lines:
        s = raw.rstrip("\n")
        if not s.strip():
            continue
        pair = _parse_srs_pair_from_bullet(s)
        if pair:
            out.append(pair)
    return out


@dataclass(frozen=True, slots=True)
class FlashcardAppendResult:
    ok: bool
    code: str
    hint: str
    dry_run: bool
    cards_preview: list[str]
    inserted_line_count: int
    path: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "code": self.code,
            "hint": self.hint,
            "dry_run": self.dry_run,
            "cards_preview": self.cards_preview,
            "inserted_line_count": self.inserted_line_count,
            "path": self.path,
        }


def append_logseq_flashcards_under_block(
    graph_root: str | Path,
    page_ref: str,
    source_block_uuid: str,
    *,
    max_cards: int = 30,
    dry_run: bool = True,
) -> FlashcardAppendResult:
    """Append ``- Q #card A`` (+ ``id::``) children after ``source_block_uuid`` subtree."""
    path, lines, err = read_page_lines(graph_root, page_ref)
    if err or path is None or lines is None:
        return FlashcardAppendResult(
            ok=False,
            code=err or "read_error",
            hint="Could not read page.",
            dry_run=dry_run,
            cards_preview=[],
            inserted_line_count=0,
            path=None,
        )

    with page_rmw_lock(path):
        stripped = [ln.rstrip("\n") for ln in lines]
        loc = locate_block_by_uuid(stripped, source_block_uuid)
        if loc is None:
            return FlashcardAppendResult(
                ok=False,
                code="uuid_not_found",
                hint="No matching `id::` for source_block_uuid.",
                dry_run=dry_run,
                cards_preview=[],
                inserted_line_count=0,
                path=str(path),
            )
        bullet_idx, id_idx, end = loc
        unit = bullet_indent_unit(lines, bullet_idx)
        bm = re.match(r"^(\s*)[-*+]\s+", stripped[bullet_idx])
        base_indent = bm.group(1) if bm else ""
        child_indent = base_indent + unit

        chunk = stripped[bullet_idx:end]
        pairs = _extract_pairs_from_block_text(chunk)[: max(1, min(max_cards, 200))]

        if not pairs:
            return FlashcardAppendResult(
                ok=False,
                code="no_pairs",
                hint=("No question :: answer patterns in this block subtree (non-property lines)."),
                dry_run=dry_run,
                cards_preview=[],
                inserted_line_count=0,
                path=str(path),
            )

        previews: list[str] = []
        insert_lines: list[str] = []
        for q, a in pairs:
            cid = str(uuid.uuid4())
            card_line = f"{child_indent}- {q} #card {a}\n"
            id_line = f"{child_indent}  id:: {cid}\n"
            insert_lines.extend([card_line, id_line])
            previews.append(f"- {q} #card {a}")

        new_lines = lines[:end] + insert_lines + lines[end:]
        new_text = "".join(new_lines)

        if dry_run:
            return FlashcardAppendResult(
                ok=True,
                code="dry_run_ok",
                hint="Re-run with dry_run=false to append cards after this block.",
                dry_run=True,
                cards_preview=previews,
                inserted_line_count=len(insert_lines),
                path=str(path),
            )

        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        atomic_write_bytes(path, new_text.encode("utf-8"))

        return FlashcardAppendResult(
            ok=True,
            code="applied",
            hint=f"Backup `{bak.name}`; appended {len(pairs)} cards.",
            dry_run=False,
            cards_preview=previews,
            inserted_line_count=len(insert_lines),
            path=str(path),
        )


__all__ = ["FlashcardAppendResult", "append_logseq_flashcards_under_block"]
