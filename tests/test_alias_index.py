"""Tests for alias indexing and resolution."""

from __future__ import annotations

from pathlib import Path

from src.graph.alias_index import (
    build_alias_index,
    collect_relevant_alias_pages,
    format_alias_index_for_prompt,
    normalize_concept_key,
)


def test_normalize_concept_key() -> None:
    assert normalize_concept_key("  [[AI]]  ") == normalize_concept_key("ai")


def test_build_alias_index_resolves_title_and_alias(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Artificial Intelligence.md").write_text(
        "type:: entity\nalias:: AI, [[A.I.]]\n",
        encoding="utf-8",
    )
    idx = build_alias_index(tmp_path)
    r_ai = idx.resolve("AI")
    assert r_ai.matched and r_ai.canonical_page_title == "Artificial Intelligence"
    assert r_ai.matched_via == "alias"
    r_full = idx.resolve("artificial intelligence")
    assert r_full.matched_via == "title"
    r_new = idx.resolve("Quantum Computing")
    assert r_new.safe_to_create_new_page is True


def test_collect_relevant_alias_pages_localizes_prompt_context(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "Artificial Intelligence.md").write_text(
        "type:: entity\nalias:: AI, [[A.I.]]\n",
        encoding="utf-8",
    )
    (pages / "Redis.md").write_text("type:: risorsa\n", encoding="utf-8")
    (pages / "PostgreSQL.md").write_text("type:: risorsa\n", encoding="utf-8")
    idx = build_alias_index(tmp_path)

    content = "- Learn about [[Redis]] caching patterns\n"
    relevant = collect_relevant_alias_pages(idx, content)
    assert relevant == {"Redis"}

    localized = format_alias_index_for_prompt(idx, page_content=content)
    assert "[[Redis]]" in localized
    assert "PostgreSQL" not in localized
    assert "Artificial Intelligence" not in localized

    full = format_alias_index_for_prompt(idx)
    assert "PostgreSQL" in full
    assert "Artificial Intelligence" in full
