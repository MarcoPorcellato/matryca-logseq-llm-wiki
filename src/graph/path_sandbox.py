"""Enforce that all graph disk paths stay inside the configured Logseq graph root."""

from __future__ import annotations

from pathlib import Path

from .page_path import page_title_to_filename

SECURITY_VIOLATION_MSG = "Security Violation: Path traversal attempt blocked."


def resolved_graph_root(graph_root: str | Path) -> Path:
    """Return the canonical absolute graph directory."""
    return Path(graph_root).expanduser().resolve(strict=False)


def assert_path_within_graph(path: Path | str, graph_root: str | Path) -> Path:
    """Resolve ``path`` and ensure it lies under ``graph_root``.

    Raises:
        ValueError: When the resolved path escapes the graph root (traversal / symlink).
    """
    root = resolved_graph_root(graph_root)
    resolved = Path(path).expanduser().resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError(SECURITY_VIOLATION_MSG)
    return resolved


def _validate_page_ref(raw: str) -> None:
    normalized = raw.strip().replace("\\", "/")
    if not normalized or ".." in normalized or normalized.startswith("/"):
        raise ValueError(SECURITY_VIOLATION_MSG)


def graph_safe_page_path(graph_root: str | Path, page_ref: str) -> Path:
    """Resolve ``page_ref`` (``pages/Foo.md`` or ``Foo``) to an absolute path under the graph."""
    root = resolved_graph_root(graph_root)
    raw = page_ref.strip().replace("\\", "/")
    _validate_page_ref(raw)
    if raw.startswith("pages/"):
        rel = raw.removeprefix("pages/")
        _validate_page_ref(rel)
        filename = rel if rel.endswith(".md") else page_title_to_filename(rel.removesuffix(".md"))
        candidate = (root / "pages" / filename).resolve(strict=False)
    else:
        filename = raw if raw.endswith(".md") else page_title_to_filename(raw.removesuffix(".md"))
        candidate = (root / "pages" / filename).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise ValueError(SECURITY_VIOLATION_MSG)
    return candidate


__all__ = [
    "SECURITY_VIOLATION_MSG",
    "assert_path_within_graph",
    "graph_safe_page_path",
    "resolved_graph_root",
]
