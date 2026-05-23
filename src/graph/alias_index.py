"""Index ``alias::`` lines across the graph for entity resolution (line/regex scan only).

Normalization is **intentionally shallow** (casefold, trim, collapse whitespace, strip
wikilink brackets): there is no fuzzy edit-distance matching and no extra dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .page_path import page_title_from_path as _page_title_from_path
from .path_sandbox import (
    PathTraversalSecurityError,
    is_resolved_path_within_graph,
    read_graph_file_text,
    resolved_graph_root,
)

_ALIAS_LINE = re.compile(r"(?im)^\s*alias::\s*(.+?)\s*$")


def normalize_concept_key(value: str) -> str:
    """Normalize a page title or alias fragment for dictionary lookup."""
    t = value.strip()
    t = t.strip("[]").strip()
    t = re.sub(r"\s+", " ", t)
    return t.casefold()


def _split_alias_segments(raw: str) -> list[str]:
    """Split ``alias::`` payload on commas (Logseq's common multi-alias form)."""
    out: list[str] = []
    for chunk in raw.split(","):
        c = chunk.strip()
        if c:
            out.append(c)
    return out


def _iter_markdown_files(graph_root: Path) -> list[Path]:
    pages = graph_root / "pages"
    journals = graph_root / "journals"
    files: list[Path] = []
    if pages.is_dir():
        files.extend(
            p for p in sorted(pages.rglob("*.md")) if is_scannable_graph_markdown(p, graph_root)
        )
    if journals.is_dir():
        files.extend(
            p for p in sorted(journals.rglob("*.md")) if is_scannable_graph_markdown(p, graph_root)
        )
    return files


MATRYCA_INTERNAL_DIR_NAMES = frozenset({".matryca_semantic_cache"})
# Logseq backup / recycle trees and VCS metadata must never enter alias or catalog scans.
EXCLUDED_GRAPH_DIR_NAMES = frozenset({"logseq", ".recycle", ".git"})


def is_scannable_graph_markdown(path: Path, graph_root: Path) -> bool:
    """Return False for hidden, internal, backup, recycle, VCS, or symlink-escape paths."""
    root = resolved_graph_root(graph_root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    for part in rel.parts:
        if part.startswith("."):
            return False
        if part in MATRYCA_INTERNAL_DIR_NAMES:
            return False
        if part in EXCLUDED_GRAPH_DIR_NAMES:
            return False
    return is_resolved_path_within_graph(path, root)


def iter_scannable_pages_markdown(graph_root: str | Path) -> list[Path]:
    """All scannable ``pages/**/*.md`` under ``graph_root`` (stable sort)."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    pages = root / "pages"
    if not pages.is_dir():
        return []
    return sorted(
        p for p in pages.rglob("*.md") if p.is_file() and is_scannable_graph_markdown(p, root)
    )


def page_title_from_path(graph_root: Path, path: Path) -> str:
    """Derive Logseq-style page title from a path under ``pages/`` or ``journals/``."""
    return _page_title_from_path(graph_root, path)


@dataclass
class AliasIndex:
    """In-memory alias map built from a single graph scan."""

    graph_root: str
    alias_to_page: dict[str, str] = field(default_factory=dict)
    page_to_aliases: dict[str, list[str]] = field(default_factory=dict)
    page_to_relpath: dict[str, str] = field(default_factory=dict)
    collision_notes: list[str] = field(default_factory=list)

    def resolve(self, candidate: str) -> ResolvedEntity:
        """Resolve one candidate against titles and aliases."""
        norm = normalize_concept_key(candidate)
        collisions: list[str] = []
        if not norm:
            return ResolvedEntity(
                candidate=candidate,
                normalized=norm,
                matched=False,
                canonical_page_title=None,
                page_relative_path=None,
                matched_via="none",
                existing_aliases=[],
                collisions=[],
                safe_to_create_new_page=False,
            )

        # Title match (page stem / journal stem as indexed)
        for title, rel in self.page_to_relpath.items():
            if normalize_concept_key(title) == norm:
                aliases = list(self.page_to_aliases.get(title, []))
                return ResolvedEntity(
                    candidate=candidate,
                    normalized=norm,
                    matched=True,
                    canonical_page_title=title,
                    page_relative_path=rel,
                    matched_via="title",
                    existing_aliases=aliases,
                    collisions=collisions,
                    safe_to_create_new_page=False,
                )

        page = self.alias_to_page.get(norm)
        if page:
            aliases = list(self.page_to_aliases.get(page, []))
            return ResolvedEntity(
                candidate=candidate,
                normalized=norm,
                matched=True,
                canonical_page_title=page,
                page_relative_path=self.page_to_relpath.get(page),
                matched_via="alias",
                existing_aliases=aliases,
                collisions=collisions,
                safe_to_create_new_page=False,
            )

        return ResolvedEntity(
            candidate=candidate,
            normalized=norm,
            matched=False,
            canonical_page_title=None,
            page_relative_path=None,
            matched_via="none",
            existing_aliases=[],
            collisions=collisions,
            safe_to_create_new_page=True,
        )


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    candidate: str
    normalized: str
    matched: bool
    canonical_page_title: str | None
    page_relative_path: str | None
    matched_via: Literal["none", "title", "alias"]
    existing_aliases: list[str]
    collisions: list[str]
    safe_to_create_new_page: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate,
            "normalized": self.normalized,
            "matched": self.matched,
            "canonical_page_title": self.canonical_page_title,
            "page_relative_path": self.page_relative_path,
            "matched_via": self.matched_via,
            "existing_aliases": list(self.existing_aliases),
            "collisions": list(self.collisions),
            "safe_to_create_new_page": self.safe_to_create_new_page,
        }


def iter_alias_source_paths(graph_root: str | Path) -> list[Path]:
    """Markdown paths scanned by :func:`build_alias_index` (stable sort)."""
    return _iter_markdown_files(Path(graph_root).expanduser().resolve(strict=False))


def build_alias_index(graph_root: str | Path) -> AliasIndex:
    """Walk ``pages/**/*.md`` and ``journals/**/*.md`` collecting ``alias::`` lines."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    idx = AliasIndex(graph_root=str(root))
    for path in _iter_markdown_files(root):
        try:
            text = read_graph_file_text(path, root)
        except (OSError, PathTraversalSecurityError) as exc:
            idx.collision_notes.append(f"unreadable {path}: {exc}")
            continue
        rel = path.relative_to(root).as_posix()
        title = page_title_from_path(root, path)
        idx.page_to_relpath.setdefault(title, rel)

        aliases_found: list[str] = []
        for match in _ALIAS_LINE.finditer(text):
            segments = _split_alias_segments(match.group(1))
            for seg in segments:
                aliases_found.append(seg)
                nk = normalize_concept_key(seg)
                if not nk:
                    continue
                existing = idx.alias_to_page.get(nk)
                if existing and existing != title:
                    idx.collision_notes.append(
                        f"alias `{seg}` ({nk!r}) maps to both `{existing}` and `{title}`; "
                        f"keeping `{existing}`",
                    )
                    continue
                if nk not in idx.alias_to_page:
                    idx.alias_to_page[nk] = title
        if aliases_found:
            idx.page_to_aliases[title] = aliases_found
    return idx


def remove_page_from_alias_index(idx: AliasIndex, page_title: str) -> None:
    """Drop all alias entries owned by ``page_title`` (for incremental cache refresh)."""
    stale_keys = [key for key, title in idx.alias_to_page.items() if title == page_title]
    for key in stale_keys:
        del idx.alias_to_page[key]
    idx.page_to_aliases.pop(page_title, None)
    idx.page_to_relpath.pop(page_title, None)


def purge_stale_alias_entries(idx: AliasIndex, live_titles: set[str]) -> int:
    """Drop alias rows whose canonical page no longer exists on disk."""
    purged = 0
    stale_titles = [title for title in list(idx.page_to_relpath) if title not in live_titles]
    for title in stale_titles:
        remove_page_from_alias_index(idx, title)
        purged += 1
    orphan_keys = [key for key, title in idx.alias_to_page.items() if title not in live_titles]
    for key in orphan_keys:
        del idx.alias_to_page[key]
        purged += 1
    return purged


def index_aliases_from_file(idx: AliasIndex, graph_root: Path, path: Path) -> None:
    """Merge ``alias::`` lines from one markdown file into an existing index."""
    root = Path(graph_root).expanduser().resolve(strict=False)
    try:
        text = read_graph_file_text(path, root)
    except (OSError, PathTraversalSecurityError) as exc:
        idx.collision_notes.append(f"unreadable {path}: {exc}")
        return
    rel = path.relative_to(root).as_posix()
    title = page_title_from_path(root, path)
    idx.page_to_relpath[title] = rel

    aliases_found: list[str] = []
    for match in _ALIAS_LINE.finditer(text):
        segments = _split_alias_segments(match.group(1))
        for seg in segments:
            aliases_found.append(seg)
            nk = normalize_concept_key(seg)
            if not nk:
                continue
            existing = idx.alias_to_page.get(nk)
            if existing and existing != title:
                idx.collision_notes.append(
                    f"alias `{seg}` ({nk!r}) maps to both `{existing}` and `{title}`; "
                    f"keeping `{existing}`",
                )
                continue
            if nk not in idx.alias_to_page:
                idx.alias_to_page[nk] = title
    if aliases_found:
        idx.page_to_aliases[title] = aliases_found


def _fold_content_for_alias_scan(content: str) -> str:
    return re.sub(r"\s+", " ", content.casefold())


def _mention_matches(content: str, content_fold: str, needle: str) -> bool:
    """Return True when ``needle`` appears as plain text or a ``[[wikilink]]``."""
    stripped = needle.strip().strip("[]").strip()
    if not stripped:
        return False
    folded_needle = normalize_concept_key(stripped)
    if len(folded_needle) < 2:
        return False
    if folded_needle in content_fold:
        return True
    if re.search(
        rf"\[\[{re.escape(stripped)}(?:\|[^\]]+)?\]\]",
        content,
        re.IGNORECASE,
    ):
        return True
    return stripped.casefold() in content_fold


def collect_relevant_alias_pages(idx: AliasIndex, content: str) -> set[str]:
    """Return canonical titles whose title or alias is mentioned in ``content``."""
    if not content.strip():
        return set()
    content_fold = _fold_content_for_alias_scan(content)
    relevant: set[str] = set()
    for title in idx.page_to_relpath:
        if _mention_matches(content, content_fold, title):
            relevant.add(title)
            continue
        for alias in idx.page_to_aliases.get(title, []):
            if _mention_matches(content, content_fold, alias):
                relevant.add(title)
                break
    return relevant


def format_alias_index_for_prompt(
    idx: AliasIndex,
    *,
    page_content: str | None = None,
    max_entries: int = 400,
) -> str:
    """Serialize canonical titles and aliases for LLM semantic-routing context."""
    lines: list[str] = []
    if page_content is not None:
        titles = sorted(collect_relevant_alias_pages(idx, page_content), key=str.casefold)
    else:
        titles = sorted(idx.page_to_relpath.keys(), key=str.casefold)
    for title in titles:
        aliases = idx.page_to_aliases.get(title, [])
        if aliases:
            alias_text = ", ".join(aliases)
            lines.append(f"- canonical: [[{title}]] | aliases: {alias_text}")
        else:
            lines.append(f"- canonical: [[{title}]]")
        if len(lines) >= max_entries:
            remaining = len(titles) - max_entries
            if remaining > 0:
                lines.append(f"- ... ({remaining} more canonical page(s) omitted)")
            break
    if not lines:
        if page_content is not None:
            return "(no AliasIndex entries match this page — prefer existing links only)"
        return "(empty graph — no canonical pages indexed yet)"
    return "\n".join(lines)


def resolve_canonical_page_title(idx: AliasIndex, candidate: str) -> str:
    """Map a wikilink target to its canonical page title when known."""
    resolved = idx.resolve(candidate)
    if resolved.matched and resolved.canonical_page_title:
        return resolved.canonical_page_title
    return candidate.strip()


__all__ = [
    "AliasIndex",
    "ResolvedEntity",
    "build_alias_index",
    "collect_relevant_alias_pages",
    "format_alias_index_for_prompt",
    "index_aliases_from_file",
    "EXCLUDED_GRAPH_DIR_NAMES",
    "is_scannable_graph_markdown",
    "iter_scannable_pages_markdown",
    "iter_alias_source_paths",
    "normalize_concept_key",
    "page_title_from_path",
    "purge_stale_alias_entries",
    "remove_page_from_alias_index",
    "resolve_canonical_page_title",
]
