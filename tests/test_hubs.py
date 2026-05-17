"""Tests for namespace hub index."""

from __future__ import annotations

from pathlib import Path

from src.config import MatrycaWikiConfig
from src.graph.hubs import build_namespace_index_markdown


def test_namespace_index_groups_triple_underscore(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Wiki___Tech___Docker.md").write_text("- x\n", encoding="utf-8")
    (pages / "Other.md").write_text("- y\n", encoding="utf-8")
    cfg = MatrycaWikiConfig()
    md = build_namespace_index_markdown(tmp_path, cfg)
    assert "Wiki" in md
    assert "Wiki/Tech/Docker" in md or "[[Wiki/Tech/Docker]]" in md
