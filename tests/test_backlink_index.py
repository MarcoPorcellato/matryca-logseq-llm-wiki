"""Tests for persistent backlink counts."""

from __future__ import annotations

from pathlib import Path

from src.graph.backlink_index import load_incoming_backlinks, patch_backlink_index_for_paths


def test_backlink_index_counts_incoming(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Target.md").write_text("- target page\n", encoding="utf-8")
    (pages / "Source.md").write_text("- see [[Target]]\n", encoding="utf-8")
    incoming = load_incoming_backlinks(tmp_path, force_rebuild=True)
    assert incoming.get("Target", 0) >= 1


def test_patch_backlink_index_invalidates_cache(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    target = pages / "Target.md"
    source = pages / "Source.md"
    target.write_text("- target\n", encoding="utf-8")
    source.write_text("- plain\n", encoding="utf-8")
    load_incoming_backlinks(tmp_path, force_rebuild=True)
    source.write_text("- links [[Target]]\n", encoding="utf-8")
    patch_backlink_index_for_paths(tmp_path, [source])
    incoming = load_incoming_backlinks(tmp_path)
    assert incoming.get("Target", 0) >= 1
