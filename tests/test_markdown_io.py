"""Tests for mmap markdown reads."""

from __future__ import annotations

from pathlib import Path

from src.graph.markdown_io import mmap_graph_page, read_graph_page_text
from src.graph.master_catalog import (
    extract_catalog_fields_from_content,
    extract_catalog_fields_from_mmap,
)


def test_read_graph_page_text_roundtrip(tmp_path: Path) -> None:
    graph = tmp_path
    page = graph / "pages" / "Demo.md"
    page.parent.mkdir(parents=True)
    body = "### Matryca Semantic Index\n- summary:: Hello world\n"
    page.write_text(body, encoding="utf-8")
    text = read_graph_page_text(page, graph)
    assert "Hello world" in text
    assert extract_catalog_fields_from_content(text) is not None


def test_extract_catalog_fields_from_mmap_matches_string(tmp_path: Path) -> None:
    graph = tmp_path
    page = graph / "pages" / "Demo.md"
    page.parent.mkdir(parents=True)
    body = "### Matryca Semantic Index\n- summary:: Mapped hello\n"
    page.write_text(body, encoding="utf-8")
    with mmap_graph_page(page, graph) as view:
        from_mmap = extract_catalog_fields_from_mmap(view)
    from_str = extract_catalog_fields_from_content(body)
    assert from_mmap is not None
    assert from_str is not None
    assert from_mmap.summary == from_str.summary
