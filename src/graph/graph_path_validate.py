"""Validation helpers for Logseq graph root paths."""

from __future__ import annotations

from pathlib import Path


def validate_logseq_graph_path(raw: str) -> Path:
    """Ensure ``raw`` resolves to a directory containing a ``pages/`` subtree."""
    cleaned = raw.strip()
    if not cleaned:
        msg = "LOGSEQ_GRAPH_PATH must be a non-empty path"
        raise ValueError(msg)
    path = Path(cleaned).expanduser().resolve(strict=False)
    if not path.is_dir():
        msg = f"LOGSEQ_GRAPH_PATH is not a directory: {path}"
        raise ValueError(msg)
    pages = path / "pages"
    if not pages.is_dir():
        msg = f"LOGSEQ_GRAPH_PATH must contain a pages/ directory: {path}"
        raise ValueError(msg)
    return path


__all__ = ["validate_logseq_graph_path"]
