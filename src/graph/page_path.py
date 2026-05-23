"""Logseq page title ↔ on-disk filename translation (semantic ``/`` vs physical ``___``)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote

# Logseq ``:file/name-format :triple-lowbar`` encodes ``/`` as ``___`` then percent-encodes
# remaining OS-reserved characters (Windows-safe cross-platform filenames).
_LOGSEQ_FILENAME_ENCODE_CHARS = frozenset('?#%<>|\\":*')


def _logseq_encode_filename_stem(stem: str) -> str:
    return "".join(
        quote(char, safe="") if char in _LOGSEQ_FILENAME_ENCODE_CHARS else char for char in stem
    )


def filename_to_page_title(filename: str) -> str:
    """Convert a markdown filename or stem to a Logseq semantic page title."""
    raw = filename.replace("\\", "/").strip()
    name = Path(raw).name
    stem = name.removesuffix(".md")
    decoded = unquote(stem)
    return decoded.replace("___", "/")


def page_title_to_filename(title: str) -> str:
    """Convert a Logseq semantic page title to an on-disk ``pages/*.md`` filename."""
    stem = title.strip().replace("\\", "/").removesuffix(".md")
    safe = stem.replace("/", "___")
    encoded = _logseq_encode_filename_stem(safe)
    return f"{encoded}.md"


def page_title_from_graph_relpath(relpath: str) -> str:
    """Derive a semantic page title from a graph-relative path (``pages/…`` or ``journals/…``)."""
    rel = Path(relpath.replace("\\", "/"))
    parts = rel.parts
    if parts and parts[0] in ("pages", "journals"):
        rel = Path(*parts[1:]) if len(parts) > 1 else Path()
    stem = rel.as_posix().removesuffix(".md") if rel.parts else ""
    return stem.replace("___", "/")


def page_title_from_path(graph_root: Path, path: Path) -> str:
    """Derive Logseq-style page title from an absolute path under the graph root."""
    rel = path.relative_to(graph_root).as_posix()
    return page_title_from_graph_relpath(rel)


def resolve_existing_page_title(graph_root: Path | str, page_title: str) -> str | None:
    """Return the canonical page title when a file or alias exists (case-insensitive)."""
    from .alias_index import is_scannable_graph_markdown
    from .generational_cache import cached_build_alias_index
    from .path_sandbox import resolved_graph_root

    root = resolved_graph_root(graph_root)
    pages_dir = root / "pages"
    if pages_dir.is_dir():
        fold = page_title.casefold()
        for candidate in pages_dir.rglob("*.md"):
            if not candidate.is_file() or not is_scannable_graph_markdown(candidate, root):
                continue
            title = filename_to_page_title(candidate.name)
            if title.casefold() == fold:
                return title

    resolved = cached_build_alias_index(root).resolve(page_title)
    if resolved.matched and resolved.canonical_page_title:
        return resolved.canonical_page_title
    return None


__all__ = [
    "filename_to_page_title",
    "page_title_from_graph_relpath",
    "page_title_from_path",
    "page_title_to_filename",
    "resolve_existing_page_title",
]
