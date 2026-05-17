"""Tests for dashboard aggregation."""

from __future__ import annotations

from pathlib import Path

from src.graph.dashboard import build_dashboard_markdown, collect_dashboard_stats


def test_collect_dashboard_stats_counts_pages_and_ids(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    uid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    (pages / "a.md").write_text(
        f"- Root\n  id:: {uid}\n  - (({uid}))\n",
        encoding="utf-8",
    )
    stats = collect_dashboard_stats(tmp_path)
    assert stats.page_count == 1
    assert stats.id_declaration_tally == 1
    assert stats.broken_block_refs == 0


def test_build_dashboard_markdown_includes_headings(tmp_path: Path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "empty.md").write_text("- x\n", encoding="utf-8")
    md = build_dashboard_markdown(tmp_path)
    assert "Matryca Dashboard" in md
    assert "render_logseq_dashboard" in md
