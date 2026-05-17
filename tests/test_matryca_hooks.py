"""Tests for Logseq spatial read adapters (``matryca_hooks``)."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.rag.matryca_hooks import get_page_spatial_context, resolve_logseq_page_md


@pytest.mark.asyncio
async def test_get_page_spatial_context_returns_markdown(tmp_path: Path) -> None:
    """A minimal ``pages/*.md`` graph should resolve and yield LLM-oriented Markdown."""
    graph = tmp_path / "graph"
    pages = graph / "pages"
    pages.mkdir(parents=True)
    sample = """- Root block
  id:: 550e8400-e29b-41d4-a716-446655440000
  - Child idea
"""
    (pages / "DemoPage.md").write_text(sample, encoding="utf-8")

    md = await get_page_spatial_context("DemoPage", str(graph))
    assert "DemoPage" in md or "Spatial view" in md
    assert "Root block" in md or "Child idea" in md


def test_resolve_logseq_page_md_raises_when_pages_missing(tmp_path: Path) -> None:
    """Missing ``pages/`` should surface as :class:`FileNotFoundError`."""
    empty = tmp_path / "nogpages"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="pages/"):
        resolve_logseq_page_md(empty, "Any")


@pytest.mark.asyncio
async def test_get_page_spatial_context_raises_when_page_missing(tmp_path: Path) -> None:
    """Unknown page titles should raise :class:`FileNotFoundError`."""
    graph = tmp_path / "g"
    (graph / "pages").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="No page markdown"):
        await get_page_spatial_context("DoesNotExist", str(graph))
