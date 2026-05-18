"""Split oversized list bullets while keeping ``id::`` on the parent block."""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from .markdown_blocks import bullet_indent_unit, read_page_lines
from .mldoc_guards import bullet_first_line_refactor_blocked, pre_id_block_lines_protected

_BULLET = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_ID_LINE = re.compile(
    r"^\s*id::\s*([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*$",
    re.IGNORECASE,
)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


@dataclass(frozen=True, slots=True)
class SplitHit:
    page_ref: str
    block_uuid: str
    before_len: int
    sentences: int

    def as_dict(self) -> dict[str, object]:
        return {
            "page_ref": self.page_ref,
            "block_uuid": self.block_uuid,
            "before_len": self.before_len,
            "sentences": self.sentences,
        }


@dataclass(frozen=True, slots=True)
class SplitLargeBlocksResult:
    ok: bool
    code: str
    hint: str
    dry_run: bool
    hits: list[dict[str, object]]
    files_touched: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "code": self.code,
            "hint": self.hint,
            "dry_run": self.dry_run,
            "hits": self.hits,
            "files_touched": self.files_touched,
        }


def _collect_candidates(
    stripped: list[str],
    *,
    min_chars: int,
) -> list[tuple[int, int, str, list[str]]]:
    """``(bullet_idx, id_line_idx, uuid, sentences)`` sorted by ``bullet_idx`` descending."""
    found: list[tuple[int, int, str, list[str]]] = []
    for i, s in enumerate(stripped):
        mid = _ID_LINE.match(s)
        if not mid:
            continue
        block_uuid = mid.group(1).lower()
        bullet_idx: int | None = None
        for j in range(i - 1, -1, -1):
            bm = _BULLET.match(stripped[j])
            if bm:
                bullet_idx = j
                break
        if bullet_idx is None:
            continue
        bm = _BULLET.match(stripped[bullet_idx])
        if not bm:
            continue
        body = bm.group(3).strip()
        if bullet_first_line_refactor_blocked(body):
            continue
        if pre_id_block_lines_protected(stripped, bullet_idx, i):
            continue
        if len(body) < min_chars:
            continue
        sents = _split_sentences(body)
        if len(sents) < 2:
            continue
        found.append((bullet_idx, i, block_uuid, sents))
    found.sort(key=lambda t: t[0], reverse=True)
    return found


def _apply_one_split(
    lines: list[str],
    stripped: list[str],
    bullet_idx: int,
    id_line_idx: int,
    sents: list[str],
) -> tuple[list[str], list[str]]:
    """Return ``(new_lines, new_stripped)`` after one split."""
    bm = _BULLET.match(stripped[bullet_idx])
    if not bm:
        return lines, stripped
    unit = bullet_indent_unit(lines, bullet_idx)
    ws, mark = bm.group(1), bm.group(2)
    child_ws = ws + unit
    first = sents[0]

    old_bullet = lines[bullet_idx]
    core = old_bullet.rstrip("\n")
    nl = old_bullet[len(core) :]
    new_bullet_core = f"{ws}{mark} {first}"
    new_bullet = new_bullet_core + nl

    extra: list[str] = []
    for sent in sents[1:]:
        cid = str(uuid.uuid4())
        extra.append(f"{child_ws}- {sent}\n")
        extra.append(f"{child_ws}  id:: {cid}\n")

    new_lines = lines[:bullet_idx] + [new_bullet] + lines[bullet_idx + 1 : id_line_idx + 1]
    new_lines.extend(extra)
    new_lines.extend(lines[id_line_idx + 1 :])
    new_stripped = [ln.rstrip("\n") for ln in new_lines]
    return new_lines, new_stripped


def _process_page(
    graph_root: Path,
    page_ref: str,
    *,
    min_chars: int,
    max_blocks: int,
    dry_run: bool,
) -> tuple[list[SplitHit], str | None, list[str]]:
    path, lines, err = read_page_lines(graph_root, page_ref)
    if err or path is None or lines is None:
        return [], err, []

    stripped = [ln.rstrip("\n") for ln in lines]
    hits: list[SplitHit] = []
    cur_lines = list(lines)
    cur_stripped = stripped

    while len(hits) < max_blocks:
        candidates = _collect_candidates(cur_stripped, min_chars=min_chars)
        if not candidates:
            break
        bullet_idx, id_line_idx, block_uuid, sents = candidates[0]
        bm0 = _BULLET.match(cur_stripped[bullet_idx])
        original_len = len(bm0.group(3).strip()) if bm0 else 0
        cur_lines, cur_stripped = _apply_one_split(
            cur_lines,
            cur_stripped,
            bullet_idx,
            id_line_idx,
            sents,
        )
        hits.append(
            SplitHit(
                page_ref=page_ref,
                block_uuid=block_uuid,
                before_len=original_len,
                sentences=len(sents),
            ),
        )

    if not hits:
        return [], None, []

    new_text = "".join(cur_lines)
    if dry_run:
        return hits, None, [str(path)]

    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)
    fd, tmp = tempfile.mkstemp(prefix="matryca-split-", suffix=".md", dir=str(path.parent))
    try:
        with open(fd, "wb", closefd=True) as fh:
            fh.write(new_text.encode("utf-8"))
        Path(tmp).replace(path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    return hits, None, [str(path)]


def refactor_large_blocks(
    graph_root: str | Path,
    *,
    page_ref: str | None = None,
    min_chars: int = 400,
    max_blocks: int = 25,
    dry_run: bool = True,
) -> SplitLargeBlocksResult:
    root = Path(graph_root).expanduser().resolve(strict=False)
    pages_dir = root / "pages"
    targets: list[str] = []
    if page_ref and page_ref.strip():
        targets.append(page_ref.strip())
    elif pages_dir.is_dir():
        targets = sorted({p.stem for p in pages_dir.glob("*.md")})
    else:
        return SplitLargeBlocksResult(
            ok=False,
            code="no_pages",
            hint="No pages/ directory or page_ref.",
            dry_run=dry_run,
            hits=[],
            files_touched=[],
        )

    all_hits: list[SplitHit] = []
    touched: list[str] = []
    remaining = max(1, min(max_blocks, 200))
    for stem in targets:
        hits, err, files = _process_page(
            root,
            stem,
            min_chars=max(50, min_chars),
            max_blocks=remaining,
            dry_run=dry_run,
        )
        if err:
            return SplitLargeBlocksResult(
                ok=False,
                code=err,
                hint="Read failed.",
                dry_run=dry_run,
                hits=[],
                files_touched=[],
            )
        all_hits.extend(hits)
        touched.extend(files)
        remaining -= len(hits)
        if page_ref:
            break
        if remaining <= 0:
            break

    if not all_hits:
        return SplitLargeBlocksResult(
            ok=True,
            code="noop",
            hint="No splittable long bullets found.",
            dry_run=dry_run,
            hits=[],
            files_touched=[],
        )

    return SplitLargeBlocksResult(
        ok=True,
        code="dry_run_ok" if dry_run else "applied",
        hint="Re-run with dry_run=false after snapshot." if dry_run else "Split applied.",
        dry_run=dry_run,
        hits=[h.as_dict() for h in all_hits],
        files_touched=touched,
    )


__all__ = ["SplitHit", "SplitLargeBlocksResult", "refactor_large_blocks"]
