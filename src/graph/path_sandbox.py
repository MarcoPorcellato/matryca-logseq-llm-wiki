"""Enforce that all graph disk paths stay inside the configured Logseq graph root."""

from __future__ import annotations

from pathlib import Path

from .page_path import page_title_to_filename

SECURITY_VIOLATION_MSG = "Security Violation: Path traversal attempt blocked."


class PathTraversalSecurityError(ValueError):
    """Raised when a resolved path escapes ``LOGSEQ_GRAPH_PATH`` (traversal or symlink)."""

    def __init__(self, message: str = SECURITY_VIOLATION_MSG) -> None:
        super().__init__(message)


def resolved_graph_root(graph_root: str | Path) -> Path:
    """Return the canonical absolute graph directory."""
    return Path(graph_root).expanduser().resolve(strict=False)


def assert_path_within_graph(path: Path | str, graph_root: str | Path) -> Path:
    """Resolve ``path`` (following symlinks) and ensure it lies under ``graph_root``.

    Raises:
        PathTraversalSecurityError: When the resolved path escapes the graph root.
    """
    root = resolved_graph_root(graph_root)
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise PathTraversalSecurityError(SECURITY_VIOLATION_MSG)
    resolved = raw.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise PathTraversalSecurityError(SECURITY_VIOLATION_MSG)
    return resolved


def is_resolved_path_within_graph(path: Path | str, graph_root: str | Path) -> bool:
    """Return whether ``path`` resolves to a location under ``graph_root``."""
    try:
        assert_path_within_graph(path, graph_root)
    except (PathTraversalSecurityError, OSError):
        return False
    return True


def read_graph_file_text(
    path: Path | str,
    graph_root: str | Path,
    *,
    encoding: str = "utf-8",
) -> str:
    """Read UTF-8 text only after the resolved path passes the graph sandbox."""
    safe = assert_path_within_graph(path, graph_root)
    return safe.read_text(encoding=encoding, errors="replace")


def graph_relative_path_key(path: Path | str, graph_root: str | Path) -> str:
    """Stable graph-relative POSIX key for daemon state and cross-platform sync."""
    root = resolved_graph_root(graph_root)
    resolved = assert_path_within_graph(path, graph_root)
    return resolved.relative_to(root).as_posix()


def resolve_graph_relative_key(graph_root: str | Path, key: str) -> Path:
    """Map a graph-relative POSIX key back to an absolute path under the graph root."""
    root = resolved_graph_root(graph_root)
    rel = Path(key.replace("\\", "/").lstrip("/"))
    if ".." in rel.parts:
        raise PathTraversalSecurityError(SECURITY_VIOLATION_MSG)
    return assert_path_within_graph(root.joinpath(*rel.parts), root)


def normalize_daemon_file_key(graph_root: str | Path, key: str) -> str:
    """Convert legacy absolute keys to graph-relative POSIX form when possible."""
    cleaned = key.replace("\\", "/").strip()
    if not cleaned:
        return cleaned
    root = resolved_graph_root(graph_root)
    candidate = Path(cleaned)
    if candidate.is_absolute():
        try:
            return candidate.expanduser().resolve(strict=False).relative_to(root).as_posix()
        except ValueError:
            return ""
    rel = Path(cleaned)
    if ".." in rel.parts:
        try:
            resolved = (root / rel).resolve(strict=False)
            root_resolved = root.resolve(strict=False)
            if not resolved.is_relative_to(root_resolved):
                return ""
            return resolved.relative_to(root_resolved).as_posix()
        except ValueError:
            return ""
    return rel.as_posix()


def _validate_page_ref(raw: str) -> None:
    normalized = raw.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise PathTraversalSecurityError(SECURITY_VIOLATION_MSG)
    if ".." in Path(normalized).parts:
        raise PathTraversalSecurityError(SECURITY_VIOLATION_MSG)


def graph_safe_page_path(graph_root: str | Path, page_ref: str) -> Path:
    """Resolve ``page_ref`` (``pages/Foo.md`` or ``Foo``) to an absolute path under the graph."""
    root = resolved_graph_root(graph_root)
    raw = page_ref.strip().replace("\\", "/")
    _validate_page_ref(raw)
    pages = root / "pages"
    if raw.startswith("pages/"):
        rel = raw.removeprefix("pages/")
        _validate_page_ref(rel)
        filename = rel if rel.endswith(".md") else page_title_to_filename(rel.removesuffix(".md"))
        candidate = pages / filename
    else:
        filename = raw if raw.endswith(".md") else page_title_to_filename(raw.removesuffix(".md"))
        candidate = pages / filename
    return assert_path_within_graph(candidate, root)


__all__ = [
    "PathTraversalSecurityError",
    "SECURITY_VIOLATION_MSG",
    "assert_path_within_graph",
    "graph_relative_path_key",
    "graph_safe_page_path",
    "is_resolved_path_within_graph",
    "normalize_daemon_file_key",
    "read_graph_file_text",
    "resolve_graph_relative_key",
    "resolved_graph_root",
]
