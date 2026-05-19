"""Scan and unify Logseq ``#tag`` spellings across Markdown files (no DB)."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .global_fence_scanner import compute_page_protected_line_indices
from .markdown_blocks import atomic_write_bytes, iter_graph_markdown_files
from .mldoc_properties import double_quoted_spans_in_value, parse_logseq_property_line
from .page_write_lock import page_rmw_lock

_TAG_RE = re.compile(r"#([\w./-]+)", re.UNICODE)


def _in_inline_code(line: str, idx: int) -> bool:
    before = line[:idx]
    n = before.count("`")
    return n % 2 == 1


def _in_wikilink(line: str, idx: int) -> bool:
    before = line[:idx]
    opens = before.count("[[")
    closes = before.count("]]")
    return opens > closes


def _property_value_quote_ranges(line: str) -> list[tuple[int, int]]:
    """Do not rewrite ``#tags`` inside quoted segments of ``key::`` property values."""
    pp = parse_logseq_property_line(line.rstrip("\n"))
    if not pp:
        return []
    return double_quoted_spans_in_value(pp.value_raw, base_offset=pp.value_start)


def _in_url_fragment(line: str, idx: int) -> bool:
    head = line[:idx]
    last_http = max(head.rfind("http://"), head.rfind("https://"))
    if last_http < 0:
        return False
    middle = head[last_http:idx]
    return "\t" not in middle and " " not in middle


def _valid_tag_match(line: str, m: re.Match[str]) -> bool:
    if m.start() > 0 and line[m.start() - 1] == "#":
        return False
    if _in_inline_code(line, m.start()):
        return False
    if _in_wikilink(line, m.start()):
        return False
    if _in_url_fragment(line, m.start()):
        return False
    for a, b in _property_value_quote_ranges(line):
        if a <= m.start() < b or a < m.end() <= b or (m.start() < a and m.end() > b):
            return False
    return True


@dataclass
class TagCluster:
    """One equivalence class (case-insensitive key)."""

    fold_key: str
    counts: dict[str, int] = field(default_factory=dict)

    def register(self, raw: str) -> None:
        self.counts[raw] = self.counts.get(raw, 0) + 1

    @property
    def canonical(self) -> str:
        if not self.counts:
            return ""
        return max(self.counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]


@dataclass(frozen=True, slots=True)
class TagLintRow:
    fold_key: str
    canonical: str
    variants: dict[str, int]
    total: int

    def as_dict(self) -> dict[str, object]:
        return {
            "fold_key": self.fold_key,
            "canonical": self.canonical,
            "variants": dict(sorted(self.variants.items(), key=lambda kv: (-kv[1], kv[0]))),
            "total": self.total,
        }


def scan_tag_clusters(graph_root: str | Path) -> list[TagLintRow]:
    root = Path(graph_root).expanduser().resolve(strict=False)
    clusters: dict[str, TagCluster] = {}
    for path in iter_graph_markdown_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            for m in _TAG_RE.finditer(line):
                if not _valid_tag_match(line, m):
                    continue
                raw = f"#{m.group(1)}"
                key = m.group(1).casefold()
                clusters.setdefault(key, TagCluster(fold_key=key)).register(raw)

    rows: list[TagLintRow] = []
    for c in clusters.values():
        if len(c.counts) < 2:
            continue
        total = sum(c.counts.values())
        rows.append(
            TagLintRow(
                fold_key=c.fold_key,
                canonical=c.canonical,
                variants=dict(c.counts),
                total=total,
            ),
        )
    rows.sort(key=lambda r: (-r.total, r.fold_key))
    return rows


def _replacement_pattern_for_variant(variant: str) -> re.Pattern[str]:
    body = re.escape(variant[1:])
    return re.compile(rf"(?<![#\w/])#({body})(?![\w/-])")


def unify_tags_in_text(
    text: str,
    mapping: dict[str, str],
    *,
    protected_line_indices: set[int] | None = None,
) -> tuple[str, int]:
    """Replace raw tag spellings with canonical forms. Returns (new_text, replacements)."""
    dead = protected_line_indices or set()
    total = 0
    chunks: list[str] = []
    pos = 0
    line_idx = 0
    for m in re.finditer(r"\r\n|\n|\r", text):
        line = text[pos : m.start()]
        nl = m.group(0)
        if line_idx in dead:
            new_line, n = line, 0
        else:
            new_line, n = _unify_tags_single_line(line, mapping)
        total += n
        chunks.append(new_line + nl)
        line_idx += 1
        pos = m.end()
    if line_idx in dead:
        tail, n = text[pos:], 0
    else:
        tail, n = _unify_tags_single_line(text[pos:], mapping)
    total += n
    chunks.append(tail)
    return "".join(chunks), total


def _unify_tags_single_line(line: str, mapping: dict[str, str]) -> tuple[str, int]:
    """Apply mapping on one line using the same guards as :func:`scan_tag_clusters`."""
    ops: list[tuple[int, int, str]] = []
    for raw, canonical in mapping.items():
        if raw == canonical:
            continue
        rx = _replacement_pattern_for_variant(raw)
        for m in rx.finditer(line):
            if not _valid_tag_match(line, m):
                continue
            ops.append((m.start(), m.end(), canonical))
    ops.sort(key=lambda t: t[0])
    merged: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, rep in ops:
        if start < last_end:
            continue
        merged.append((start, end, rep))
        last_end = end
    if not merged:
        return line, 0
    s = line
    for start, end, rep in sorted(merged, key=lambda t: -t[0]):
        s = s[:start] + rep + s[end:]
    return s, len(merged)


@dataclass(frozen=True, slots=True)
class TagUnifyFileResult:
    relative_path: str
    replacements: int
    preview: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "replacements": self.replacements,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class TagUnifyRunResult:
    ok: bool
    code: str
    hint: str
    dry_run: bool
    clusters: list[dict[str, object]]
    files: list[dict[str, object]]
    total_replacements: int

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "code": self.code,
            "hint": self.hint,
            "dry_run": self.dry_run,
            "clusters": self.clusters,
            "files": self.files,
            "total_replacements": self.total_replacements,
        }


def lint_unify_logseq_tags(
    graph_root: str | Path,
    *,
    dry_run: bool = True,
    max_files_preview: int = 15,
) -> TagUnifyRunResult:
    root = Path(graph_root).expanduser().resolve(strict=False)
    rows = scan_tag_clusters(root)
    mapping: dict[str, str] = {}
    for r in rows:
        for variant in r.variants:
            if variant != r.canonical:
                mapping[variant] = r.canonical

    if not mapping:
        return TagUnifyRunResult(
            ok=True,
            code="noop",
            hint="No duplicate tag spellings found (case/variant clusters).",
            dry_run=dry_run,
            clusters=[x.as_dict() for x in rows],
            files=[],
            total_replacements=0,
        )

    file_results: list[TagUnifyFileResult] = []
    total_rep = 0
    preview_count = 0
    for path in iter_graph_markdown_files(root):
        with page_rmw_lock(path):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                file_results.append(
                    TagUnifyFileResult(
                        relative_path=path.relative_to(root).as_posix(),
                        replacements=0,
                        preview=f"_read_error: {exc}_",
                    ),
                )
                continue
            protected = compute_page_protected_line_indices(raw)
            new_text, n = unify_tags_in_text(raw, mapping, protected_line_indices=protected)
            if n == 0:
                continue
            rel = path.relative_to(root).as_posix()
            preview = None
            if preview_count < max_files_preview:
                preview = f"_{n} replacement(s) in `{rel}`_"
                preview_count += 1
            file_results.append(
                TagUnifyFileResult(relative_path=rel, replacements=n, preview=preview),
            )
            total_rep += n
            if not dry_run:
                bak = path.with_suffix(path.suffix + ".bak")
                shutil.copy2(path, bak)
                atomic_write_bytes(path, new_text.encode("utf-8"))

    if dry_run and total_rep == 0:
        return TagUnifyRunResult(
            ok=True,
            code="dry_run_ok",
            hint="Mapping exists but no file occurrences (unexpected).",
            dry_run=True,
            clusters=[x.as_dict() for x in rows],
            files=[f.as_dict() for f in file_results],
            total_replacements=0,
        )

    return TagUnifyRunResult(
        ok=True,
        code="dry_run_ok" if dry_run else "applied",
        hint=(
            "Re-run with dry_run=false to rewrite files (with .bak)."
            if dry_run
            else "Tag unify applied."
        ),
        dry_run=dry_run,
        clusters=[x.as_dict() for x in rows],
        files=[f.as_dict() for f in file_results if f.replacements > 0],
        total_replacements=total_rep,
    )


__all__ = [
    "TagLintRow",
    "TagUnifyFileResult",
    "TagUnifyRunResult",
    "lint_unify_logseq_tags",
    "scan_tag_clusters",
    "unify_tags_in_text",
]
