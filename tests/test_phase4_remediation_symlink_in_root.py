"""Symlinks that resolve inside the graph root are allowed."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.graph.path_sandbox import assert_path_within_graph, read_graph_file_text


def test_assert_path_within_graph_allows_in_root_symlink(tmp_path: Path) -> None:
    graph = tmp_path / "graph"
    pages = graph / "pages"
    pages.mkdir(parents=True)
    canonical = pages / "Canonical.md"
    canonical.write_text("- inside\n", encoding="utf-8")
    link = pages / "Link.md"
    try:
        link.symlink_to(canonical)
    except (OSError, NotImplementedError):
        pytest.skip("Platform does not support symlinks in this environment")
    resolved = assert_path_within_graph(link, graph)
    assert resolved == canonical.resolve()


def test_read_graph_file_text_follows_in_root_symlink(tmp_path: Path) -> None:
    graph = tmp_path / "graph"
    pages = graph / "pages"
    pages.mkdir(parents=True)
    canonical = pages / "Canonical.md"
    canonical.write_text("- linked content\n", encoding="utf-8")
    link = pages / "Link.md"
    try:
        link.symlink_to(canonical)
    except (OSError, NotImplementedError):
        pytest.skip("Platform does not support symlinks in this environment")
    text = read_graph_file_text(link, graph)
    assert "linked content" in text
