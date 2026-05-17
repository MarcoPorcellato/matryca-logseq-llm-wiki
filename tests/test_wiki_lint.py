"""Tests for wiki-style lint on prefixed Logseq pages."""

from __future__ import annotations

from pathlib import Path

from src.config import MatrycaWikiConfig
from src.graph.wiki_lint import lint_wiki_prefixed_pages


def test_lint_flags_missing_type(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Matryca___Test.md").write_text("- hello\n", encoding="utf-8")
    cfg = MatrycaWikiConfig(wiki_file_prefix="Matryca___")
    findings = lint_wiki_prefixed_pages(tmp_path, cfg)
    rules = {f.rule for f in findings}
    assert "missing_type" in rules


def test_lint_skips_non_prefixed_pages(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Journal.md").write_text("- x\n", encoding="utf-8")
    cfg = MatrycaWikiConfig(wiki_file_prefix="Matryca___")
    assert lint_wiki_prefixed_pages(tmp_path, cfg) == []
