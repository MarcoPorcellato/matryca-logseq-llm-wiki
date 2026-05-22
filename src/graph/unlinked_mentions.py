"""Find plain-text mentions of existing page titles (upgrade candidates for wikilinks)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .global_fence_scanner import compute_page_protected_line_indices
from .markdown_blocks import iter_graph_markdown_files
from .page_path import filename_to_page_title


def _page_titles_from_graph(graph_root: Path) -> list[str]:
    titles: set[str] = set()
    pages = graph_root / "pages"
    if pages.is_dir():
        for p in pages.glob("*.md"):
            titles.add(filename_to_page_title(p.name))
    return sorted(titles, key=len, reverse=True)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    s = sorted(ranges)
    out = [s[0]]
    for a, b in s[1:]:
        la, lb = out[-1]
        if a <= lb:
            out[-1] = (la, max(lb, b))
        else:
            out.append((a, b))
    return out


def _excluded_ranges(line: str) -> list[tuple[int, int]]:
    ex: list[tuple[int, int]] = []
    # wikilinks [[ ... ]]
    for m in re.finditer(r"\[\[([^\]]+)\]\]", line):
        ex.append((m.start(), m.end()))
    # block refs ((uuid))
    for m in re.finditer(r"\(\([0-9a-fA-F-]{36}\)\)", line):
        ex.append((m.start(), m.end()))
    # inline code `...`
    for m in re.finditer(r"`[^`]*`", line):
        ex.append((m.start(), m.end()))
    # URLs
    for m in re.finditer(r"https?://[^\s)>\]]+", line):
        ex.append((m.start(), m.end()))
    # Logseq / mldoc-style macros ``{{...}}``
    for m in re.finditer(r"\{\{[\s\S]*?\}\}", line):
        ex.append((m.start(), m.end()))
    return _merge_ranges(ex)


def _in_ranges(idx: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= idx < b for a, b in ranges)


def _title_pattern(title: str) -> re.Pattern[str]:
    parts = [p for p in re.split(r"\s+", title.strip()) if p]
    if not parts:
        return re.compile(r"^$")
    inner = r"\s+".join(re.escape(p) for p in parts)
    return re.compile(rf"(?<![\w#/]){inner}(?![\w#/])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class UnlinkedHit:
    relative_path: str
    line_number: int
    column: int
    title: str
    context: str
    suggested: str

    def as_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "line_number": self.line_number,
            "column": self.column,
            "title": self.title,
            "context": self.context,
            "suggested": self.suggested,
        }


def resolve_unlinked_mentions(
    graph_root: str | Path,
    *,
    max_hits_per_file: int = 80,
    max_titles: int = 500,
) -> dict[str, object]:
    root = Path(graph_root).expanduser().resolve(strict=False)
    titles = _page_titles_from_graph(root)[: max(1, min(max_titles, 5000))]
    hits: list[UnlinkedHit] = []
    files_scanned = 0
    for path in iter_graph_markdown_files(root):
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        protected = compute_page_protected_line_indices(text)
        per_file = 0
        for li, line in enumerate(text.splitlines(), start=1):
            if per_file >= max_hits_per_file:
                break
            if li - 1 in protected:
                continue
            excl = _excluded_ranges(line)
            consumed = [False] * len(line)
            pos = 0
            while pos < len(line) and per_file < max_hits_per_file:
                progressed = False
                for title in titles:
                    rx = _title_pattern(title)
                    m = rx.search(line, pos)
                    if not m:
                        continue
                    if any(consumed[m.start() : m.end()]):
                        continue
                    if _in_ranges(m.start(), excl) or _in_ranges(m.end() - 1, excl):
                        continue
                    hits.append(
                        UnlinkedHit(
                            relative_path=rel,
                            line_number=li,
                            column=m.start() + 1,
                            title=title,
                            context=line.strip()[:240],
                            suggested=f"[[{title}]]",
                        ),
                    )
                    for k in range(m.start(), m.end()):
                        if k < len(consumed):
                            consumed[k] = True
                    per_file += 1
                    progressed = True
                    pos = m.end()
                    break
                if not progressed:
                    pos += 1
                if len(hits) >= 2000:
                    return {
                        "ok": True,
                        "files_scanned": files_scanned,
                        "hit_count": len(hits),
                        "hits": [h.as_dict() for h in hits],
                    }
    return {
        "ok": True,
        "files_scanned": files_scanned,
        "hit_count": len(hits),
        "hits": [h.as_dict() for h in hits[:2000]],
    }


__all__ = ["UnlinkedHit", "resolve_unlinked_mentions"]
