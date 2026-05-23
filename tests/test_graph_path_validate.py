"""Tests for Logseq graph path validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.graph.graph_path_validate import validate_logseq_graph_path


def test_validate_logseq_graph_path_accepts_valid_graph(tmp_path: Path) -> None:
    graph = tmp_path / "graph"
    (graph / "pages").mkdir(parents=True)
    resolved = validate_logseq_graph_path(str(graph))
    assert resolved == graph.resolve()


def test_validate_logseq_graph_path_rejects_missing_pages(tmp_path: Path) -> None:
    graph = tmp_path / "graph"
    graph.mkdir()
    with pytest.raises(ValueError, match="pages/"):
        validate_logseq_graph_path(str(graph))
