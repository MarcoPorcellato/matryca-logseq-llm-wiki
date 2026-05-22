"""Tests for Logseq page title ↔ filename translation."""

from __future__ import annotations

from pathlib import Path

from src.graph.page_path import (
    filename_to_page_title,
    page_title_from_path,
    page_title_to_filename,
    resolve_existing_page_title,
)
from src.graph.path_sandbox import graph_safe_page_path


def test_filename_to_page_title_converts_namespace() -> None:
    assert filename_to_page_title("progetti___Lancio.md") == "progetti/Lancio"
    assert filename_to_page_title("a___b___c.md") == "a/b/c"


def test_page_title_to_filename_converts_namespace() -> None:
    assert page_title_to_filename("progetti/Lancio") == "progetti___Lancio.md"
    assert page_title_to_filename("a/b/c") == "a___b___c.md"


def test_graph_safe_page_path_resolves_semantic_title(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Domain___Subdomain.md").write_text("- hub\n", encoding="utf-8")
    path = graph_safe_page_path(tmp_path, "Domain/Subdomain")
    assert path.name == "Domain___Subdomain.md"
    assert path.is_file()


def test_page_title_from_path_uses_semantic_slash(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    page = pages / "Wiki___Topic.md"
    page.write_text("- note\n", encoding="utf-8")
    assert page_title_from_path(tmp_path, page) == "Wiki/Topic"


def test_resolve_existing_page_title_is_case_insensitive(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "Machine Learning.md").write_text("- note\n", encoding="utf-8")
    assert resolve_existing_page_title(tmp_path, "Machine Learning") == "Machine Learning"
    assert resolve_existing_page_title(tmp_path, "MACHINE LEARNING") == "Machine Learning"
    assert resolve_existing_page_title(tmp_path, "machine learning") == "Machine Learning"
    assert resolve_existing_page_title(tmp_path, "Missing Page") is None
