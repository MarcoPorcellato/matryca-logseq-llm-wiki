"""Logseq page title ↔ on-disk filename translation (semantic ``/`` vs physical ``___``)."""

from __future__ import annotations

from pathlib import Path


def filename_to_page_title(filename: str) -> str:
    """Convert a markdown filename or stem to a Logseq semantic page title."""
    raw = filename.replace("\\", "/").strip()
    name = Path(raw).name
    stem = name.removesuffix(".md")
    return stem.replace("___", "/")


def page_title_to_filename(title: str) -> str:
    """Convert a Logseq semantic page title to an on-disk ``pages/*.md`` filename."""
    stem = title.strip().replace("\\", "/").removesuffix(".md")
    safe = stem.replace("/", "___")
    return f"{safe}.md"


def page_title_from_graph_relpath(relpath: str) -> str:
    """Derive a semantic page title from a graph-relative path (``pages/…`` or ``journals/…``)."""
    normalized = relpath.replace("\\", "/").removesuffix(".md")
    if normalized.startswith("pages/"):
        normalized = normalized.removeprefix("pages/")
    elif normalized.startswith("journals/"):
        normalized = normalized.removeprefix("journals/")
    return normalized.replace("___", "/")


def page_title_from_path(graph_root: Path, path: Path) -> str:
    """Derive Logseq-style page title from an absolute path under the graph root."""
    rel = path.relative_to(graph_root).as_posix()
    return page_title_from_graph_relpath(rel)


def resolve_existing_page_title(graph_root: Path | str, page_title: str) -> str | None:
    """Return the canonical on-disk page title when a file exists (case-insensitive)."""
    from .path_sandbox import resolved_graph_root

    root = resolved_graph_root(graph_root)
    pages_dir = root / "pages"
    if not pages_dir.is_dir():
        return None
    fold = page_title.casefold()
    for candidate in pages_dir.rglob("*.md"):
        if not candidate.is_file():
            continue
        title = filename_to_page_title(candidate.name)
        if title.casefold() == fold:
            return title
    return None


__all__ = [
    "filename_to_page_title",
    "page_title_from_graph_relpath",
    "page_title_from_path",
    "page_title_to_filename",
    "resolve_existing_page_title",
]
